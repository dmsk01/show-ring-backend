"""
Роутер задач (объединённый: legacy учебный + этап 8 DB-tasks).

Маршруты:
- POST /tasks/send                — учебный publish в очередь (in-memory storage).
- PUT  /tasks/{id}/status         — учебный update от воркера (legacy book_handler).
- GET  /tasks/{id}                — статус задачи. Сначала ищем в БД (этап 8),
                                    fallback на in-memory (legacy).
- GET  /tasks/{id}/download       — скачать PDF из MinIO по file_id из task.result.

Зачем смешивать legacy и DB:
- Старый учебный пример с book_handler работает через in-memory storage.
- Новые задачи генерации документов — через БД.
- Один публичный путь /tasks/{id} удобнее для клиента: он не должен знать,
  какая задача через какой механизм идёт. В коде сначала проверяем БД,
  если нет — отдаём legacy.
"""

from __future__ import annotations

import secrets
import uuid
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.dependencies import get_current_user
from app.models.file import UploadedFile
from app.models.task import TaskStatusEnum
from app.models.user import User
from app.repositories import task as task_repo
from app.schemas.task import (
    StatusUpdateRequest,
    TaskResponse,
    TaskStatusResponse,
)
from app.services import file_storage
from app.services.rabbit import rabbit_service
from app.services.task_storage import task_storage


class TaskSendRequest(BaseModel):
    message: str


router = APIRouter(prefix="/tasks", tags=["tasks"])


def verify_internal_key(x_api_key: str = Header(default="")):
    # secrets.compare_digest — защита от timing attack при сравнении ключей.
    expected = settings.internal_api_key
    if not expected or not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=403, detail="Invalid API key")


# ---------------------------------------------------------------------
# Legacy (учебный пример book_handler)
# ---------------------------------------------------------------------


@router.post("/send", dependencies=[Depends(verify_internal_key)])
async def send_task(body: TaskSendRequest):
    # Учебный эндпоинт для книжного хендлера — оставлен ради
    # совместимости с примером из этапов 1–2.
    await rabbit_service.publish("tasks", body.message)
    return {"status": "Message sent to RabbitMQ", "message": body.message}


@router.put("/{task_id}/status", dependencies=[Depends(verify_internal_key)])
def update_task_status(task_id: str, request: StatusUpdateRequest):
    # Legacy update — пишет в in-memory storage. Новые задачи (этап 8)
    # обновляют статус сами в БД, через app.repositories.task.
    response = task_storage.update_status(
        task_id, request.status, request.result, request.error
    )
    return response


# ---------------------------------------------------------------------
# DB-backed (этап 8)
# ---------------------------------------------------------------------


@router.get(
    "/{task_id}",
    summary="Статус задачи",
    description=(
        "Возвращает статус задачи. Сначала ищем в БД (новые задачи генерации "
        "документов), при отсутствии — fallback на in-memory storage (legacy)."
    ),
)
async def get_task_status(
    task_id: str,
    db: AsyncSession = Depends(get_db),
) -> TaskResponse | TaskStatusResponse:
    # 1. БД-задачи (этап 8). UUID-проверка через try/except: если task_id
    # не UUID, это точно legacy-задача с произвольной строкой ID.
    try:
        uid = uuid.UUID(task_id)
    except ValueError:
        uid = None

    if uid is not None:
        db_task = await task_repo.get_task(db, uid)
        if db_task is not None:
            return TaskResponse.model_validate(db_task)

    # 2. Legacy in-memory.
    legacy = task_storage.get_status(task_id)
    if legacy is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return legacy


def _is_admin(user: User) -> bool:
    """Локальный helper: у юзера есть роль admin."""
    return any(r.role.value == "admin" for r in user.roles)


@router.get(
    "/{task_id}/download",
    summary="Скачать результат задачи (PDF)",
)
async def download_task_result(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    Возвращает файл из MinIO по file_id из task.result.

    StreamingResponse, а не FileResponse — файлы могут быть большими
    (каталог на тысячу собак — десятки страниц), стриминг экономит память.
    """
    task = await task_repo.get_task(db, task_id)
    if task is None:
        raise HTTPException(404, "task_not_found")
    # ИСПРАВЛЕНО (bug_201): IDOR — раньше любой авторизованный мог
    # скачать чужой PDF по task_id (комментарий "пока без ACL"). Теперь
    # доступ — только автору задачи или admin. created_by IS NULL —
    # fail-closed: исторические задачи без автора недоступны никому,
    # кроме admin (избегаем «забытого» 0-owner public ресурса).
    if not _is_admin(user) and task.created_by != user.id:
        raise HTTPException(403, "forbidden")
    if task.status != TaskStatusEnum.done:
        raise HTTPException(409, "task_not_done")
    result = task.result or {}
    file_id = result.get("file_id")
    if not file_id:
        raise HTTPException(404, "result_file_missing")

    try:
        file_uuid = uuid.UUID(str(file_id))
    except ValueError:
        raise HTTPException(400, "invalid_file_id") from None

    db_file = await db.get(UploadedFile, file_uuid)
    if db_file is None:
        raise HTTPException(404, "file_not_found")

    # Получаем байты из MinIO. StreamingResponse, а не Response, потому
    # что PDF каталога на большой выставке весит мегабайты — стримим
    # клиенту чанками вместо удержания всего в памяти.
    body, content_type = await file_storage.get_file_stream(db_file.s3_key)

    def _iter():
        # Один чанк для совместимости со StreamingResponse. Когда
        # get_file_stream научится отдавать iter_chunks (этап 14), эта
        # функция получит реальный стрим вместо одного фрейма.
        yield body

    # ИСПРАВЛЕНО (bug_202): сериализуем имя файла через RFC 6266
    # filename* — без этого \r\n или " внутри original_filename
    # вылезали как инжекция произвольных HTTP-заголовков (Set-Cookie,
    # CSP-override и т.п.) на скачивающего клиента. urllib.quote с
    # safe="" экранирует ВСЁ, включая UTF-8 байты — RFC 6266 для этого
    # как раз и предлагает filename*=UTF-8''<percent-encoded>.
    safe_name = quote(db_file.original_filename or "file", safe="")
    return StreamingResponse(
        _iter(),
        media_type=content_type or db_file.content_type or "application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{safe_name}",
        },
    )
