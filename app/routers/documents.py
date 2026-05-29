"""
Роутер генерации документов (этап 8).

Что внутри:
- POST /shows/{id}/catalog/generate     — каталог выставки.
- POST /shows/{id}/diplomas/generate    — пакет дипломов одним PDF.
- POST /shows/{id}/entries/{eid}/diploma — диплом одного участника.

Шаги под капотом:
1. Создаём Task в БД со status=pending.
2. Публикуем сообщение в RabbitMQ (durable очередь, persistent message).
3. Возвращаем task_id. Клиент опрашивает /tasks/{id} до status=done.

Скачивание готового PDF — через /tasks/{id}/download (см. routers/tasks.py).
"""

from __future__ import annotations

import logging
import uuid
from typing import NoReturn

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.repositories import show as show_repo
from app.repositories import task as task_repo
from app.schemas.task import DocumentKind, TaskMessage, TaskResponse
from app.services import document_official
from app.services.document import to_jsonable
from app.services.rabbit import rabbit_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/shows", tags=["documents"])

# Имя очереди задач генерации документов. Все типы задач этой подсистемы
# идут в одну очередь — воркер диспатчит по полю type. Если бы для разных
# типов было разное SLA, имело бы смысл разделить очереди.
DOCUMENT_TASK_QUEUE = "document_task"


def _is_organizer_or_admin(user: User, organizer_id: uuid.UUID) -> bool:
    if any(r.role.value == "admin" for r in user.roles):
        return True
    return user.id == organizer_id


def _raise_for_error(err: ValueError) -> NoReturn:
    code = str(err)
    if code == "not_found":
        raise HTTPException(404, code)
    if code == "forbidden":
        raise HTTPException(403, code)
    raise HTTPException(400, code)


async def _ensure_organizer(
    db: AsyncSession, show_id: uuid.UUID, user: User
) -> None:
    show = await show_repo.get_show(db, show_id)
    if show is None:
        raise ValueError("not_found")
    if not _is_organizer_or_admin(user, show.organizer_id):
        raise ValueError("forbidden")


async def _publish_task(
    db: AsyncSession,
    user: User,
    kind: DocumentKind,
    payload: dict,
) -> TaskResponse:
    """
    Общая часть для всех типов задач:
    1. INSERT Task(pending).
    2. PUBLISH в очередь.
    3. Возврат TaskResponse.

    Если publish упадёт (rabbit недоступен) — задача останется в pending
    в БД, можно retry'ить позже. Это лучше, чем 500 без следа.
    """
    task = await task_repo.create_task(
        db,
        type_=kind.value,
        payload=payload,
        created_by=user.id,
    )
    message = TaskMessage(
        task_id=task.id, action=kind.value, payload=payload
    ).to_json()
    try:
        await rabbit_service.publish(DOCUMENT_TASK_QUEUE, message)
    except Exception as e:  # noqa: BLE001
        # Не падаем на HTTP-уровне: задача в БД, можно перепубликовать
        # через отдельный admin-эндпоинт (будет добавлен на этапе 14).
        logger.warning(
            "Failed to publish task %s to RabbitMQ: %s", task.id, e
        )
    return TaskResponse.model_validate(task)


# ---------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------


@router.post(
    "/{show_id}/catalog/generate",
    response_model=TaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Запустить генерацию каталога выставки",
)
async def generate_catalog(
    show_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        await _ensure_organizer(db, show_id, user)
    except ValueError as e:
        _raise_for_error(e)
    return await _publish_task(
        db, user, DocumentKind.CATALOG, {"show_id": str(show_id)}
    )


@router.post(
    "/{show_id}/diplomas/generate",
    response_model=TaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Запустить генерацию пакета дипломов для всех участников",
)
async def generate_diplomas(
    show_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        await _ensure_organizer(db, show_id, user)
    except ValueError as e:
        _raise_for_error(e)
    return await _publish_task(
        db, user, DocumentKind.DIPLOMAS_BATCH, {"show_id": str(show_id)}
    )


@router.post(
    "/{show_id}/entries/{entry_id}/diploma",
    response_model=TaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Сгенерировать диплом для одного участника",
)
async def generate_diploma(
    show_id: uuid.UUID,
    entry_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        await _ensure_organizer(db, show_id, user)
    except ValueError as e:
        _raise_for_error(e)
    return await _publish_task(
        db,
        user,
        DocumentKind.DIPLOMA,
        {"show_id": str(show_id), "entry_id": str(entry_id)},
    )


# ---------------------------------------------------------------------
# Официальные документы (формат РКФ) — отдельные ручки рядом со старыми.
# Формат вывода — query-параметр format=docx|pdf, кладётся в payload задачи.
# ---------------------------------------------------------------------


def _norm_format(fmt: str) -> str:
    if fmt not in ("docx", "pdf"):
        raise HTTPException(400, "format must be docx or pdf")
    return fmt


@router.post(
    "/{show_id}/official/catalog",
    response_model=TaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Каталог выставки в формате РКФ (docx/pdf)",
)
async def generate_official_catalog(
    show_id: uuid.UUID,
    format: str = "docx",
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    fmt = _norm_format(format)
    try:
        await _ensure_organizer(db, show_id, user)
    except ValueError as e:
        _raise_for_error(e)
    return await _publish_task(
        db, user, DocumentKind.CATALOG_OFFICIAL,
        {"show_id": str(show_id), "format": fmt},
    )


@router.post(
    "/{show_id}/official/diplomas",
    response_model=TaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Пакет дипломов в формате РКФ (docx/pdf)",
)
async def generate_official_diplomas(
    show_id: uuid.UUID,
    format: str = "docx",
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    fmt = _norm_format(format)
    try:
        await _ensure_organizer(db, show_id, user)
    except ValueError as e:
        _raise_for_error(e)
    return await _publish_task(
        db, user, DocumentKind.DIPLOMAS_BATCH_OFFICIAL,
        {"show_id": str(show_id), "format": fmt},
    )


@router.post(
    "/{show_id}/entries/{entry_id}/official/diploma",
    response_model=TaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Диплом участника в формате РКФ (docx/pdf)",
)
async def generate_official_diploma(
    show_id: uuid.UUID,
    entry_id: uuid.UUID,
    format: str = "docx",
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    fmt = _norm_format(format)
    try:
        await _ensure_organizer(db, show_id, user)
    except ValueError as e:
        _raise_for_error(e)
    return await _publish_task(
        db, user, DocumentKind.DIPLOMA_OFFICIAL,
        {"show_id": str(show_id), "entry_id": str(entry_id), "format": fmt},
    )


@router.post(
    "/{show_id}/official/ring-sheets",
    response_model=TaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ринговые ведомости в формате РКФ (docx/pdf)",
)
async def generate_official_ring_sheets(
    show_id: uuid.UUID,
    format: str = "docx",
    ring_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    fmt = _norm_format(format)
    try:
        await _ensure_organizer(db, show_id, user)
    except ValueError as e:
        _raise_for_error(e)
    payload = {"show_id": str(show_id), "format": fmt}
    if ring_id is not None:
        payload["ring_id"] = str(ring_id)
    return await _publish_task(
        db, user, DocumentKind.RING_SHEETS_OFFICIAL, payload
    )


# ---------------------------------------------------------------------
# Удобство фронта: предпросмотр собранных данных и чек-лист готовности.
# ---------------------------------------------------------------------


@router.get(
    "/{show_id}/official/{kind}/context",
    summary="Данные документа для предпросмотра/правки на фронте",
)
async def get_official_context(
    show_id: uuid.UUID,
    kind: str,
    entry_id: uuid.UUID | None = None,
    ring_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        await _ensure_organizer(db, show_id, user)
    except ValueError as e:
        _raise_for_error(e)
    try:
        if kind == "catalog":
            ctx = await document_official.build_catalog_context(db, show_id)
        elif kind == "ring-sheets":
            ctx = await document_official.build_ring_sheets_context(
                db, show_id, ring_id
            )
        elif kind == "diploma":
            if entry_id is None:
                raise HTTPException(400, "entry_id required for diploma")
            ctx = await document_official.build_diploma_context(db, entry_id)
        else:
            raise HTTPException(404, "unknown document kind")
    except ValueError as e:
        _raise_for_error(e)
    return to_jsonable(ctx)


@router.get(
    "/{show_id}/documents/readiness",
    summary="Чек-лист пробелов перед печатью документов",
)
async def get_documents_readiness(
    show_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        await _ensure_organizer(db, show_id, user)
        data = await document_official.build_documents_readiness(db, show_id)
    except ValueError as e:
        _raise_for_error(e)
    return data
