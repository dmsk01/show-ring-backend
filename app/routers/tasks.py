"""
Роутер задач генерации документов (этап 8, DB-backed).

Маршруты:
- GET  /tasks/{id}           — статус задачи (автор или admin).
- GET  /tasks/{id}/download  — скачать PDF из MinIO по file_id из task.result.

Ревью 2026-10-06, BE-35: учебные POST /tasks/send и PUT /tasks/{id}/status
с in-memory хранилищем удалены — состояние в памяти процесса на нескольких
uvicorn-воркерах было неконсистентным.
"""

from __future__ import annotations

import uuid
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user, is_admin
from app.models.file import UploadedFile
from app.models.task import TaskStatusEnum
from app.models.user import User
from app.repositories import task as task_repo
from app.schemas.task import (
    TaskResponse,
)
from app.services import file_storage


router = APIRouter(prefix="/tasks", tags=["tasks"])


# ---------------------------------------------------------------------
# DB-backed (этап 8)
# ---------------------------------------------------------------------


@router.get(
    "/{task_id}",
    summary="Статус задачи",
    description=(
        "Возвращает статус задачи генерации документа. Доступно автору "
        "задачи или admin."
    ),
)
async def get_task_status(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> TaskResponse:
    db_task = await task_repo.get_task(db, task_id)
    if db_task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    # ИСПРАВЛЕНО (ревью безопасности 2026-10-03, #14): ручка была
    # публичной и отдавала payload/result/created_by любому, кто
    # знает UUID задачи. ACL — как у /download: автор или admin.
    if not is_admin(user) and db_task.created_by != user.id:
        raise HTTPException(403, "forbidden")
    return TaskResponse.model_validate(db_task)


# ИСПРАВЛЕНО (review 2026-05-28): см. routers/classifieds.py.
_is_admin = is_admin


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

    # ИСПРАВЛЕНО (review 2026-06-01): реальный стриминг. Раньше
    # get_file_stream выкачивал весь PDF/DOCX в память и StreamingResponse
    # отдавал его одним кадром — никакой экономии памяти. Теперь
    # stat_file заранее проверяет существование (чистый 404 ДО отправки
    # заголовков), а iter_file стримит объект из MinIO чанками по 64 КБ.
    # content_type берём из БД — он известен без обращения к S3.
    await file_storage.stat_file(db_file.s3_key)

    # ИСПРАВЛЕНО (bug_202): сериализуем имя файла через RFC 6266
    # filename* — без этого \r\n или " внутри original_filename
    # вылезали как инжекция произвольных HTTP-заголовков (Set-Cookie,
    # CSP-override и т.п.) на скачивающего клиента. urllib.quote с
    # safe="" экранирует ВСЁ, включая UTF-8 байты — RFC 6266 для этого
    # как раз и предлагает filename*=UTF-8''<percent-encoded>.
    safe_name = quote(db_file.original_filename or "file", safe="")
    return StreamingResponse(
        file_storage.iter_file(db_file.s3_key),
        media_type=db_file.content_type or "application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{safe_name}",
        },
    )
