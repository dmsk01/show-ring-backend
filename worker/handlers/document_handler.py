"""
Хендлер задач генерации документов (этап 8).

Принимает сообщение из очереди document_task, забирает задачу из БД,
формирует PDF, заливает в MinIO, обновляет статус и result.

Зачем отдельный модуль (а не всё в worker/main.py):
- Воркер может расти на десятки типов задач — иметь по одному хендлеру
  на тип читается лучше, чем гигантский if/elif в одном месте.
- Хендлер тестируется отдельно от транспорта (моки сессии БД и
  rabbit-канала).
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.file import UploadedFile
from app.models.task import TaskStatusEnum
from app.repositories import task as task_repo
from app.schemas.task import DocumentKind
from app.services import document, file_storage
from app.utils import pdf

logger = logging.getLogger(__name__)

# bug_234 audit 2026-05-28: poison-payload (например, неудалимая
# ссылка в payload или специфический шаблон, который ломает рендерер)
# крутился вечно. Scheduler re-публиковал stuck-задачу раз в час, а
# в handler'е не было барьера: 24 рестарта в сутки до бесконечности.
# 5 попыток = ~5 часов реальных retry'ев; этого достаточно для
# восстановления после transient-сбоев (MinIO коротко недоступен,
# PG в read-only), но не позволяет poison'у жить вечно.
MAX_TASK_ATTEMPTS = 5


async def process_document_task(
    db: AsyncSession, task_id: uuid.UUID
) -> None:
    """
    Главная точка входа для хендлера. Шаги:
    1. Захватываем задачу (pending → processing). Если кто-то другой
       уже забрал — сразу return.
    2. По task.type диспатчим на конкретный generator.
    3. PDF → bytes → upload_bytes → запись UploadedFile в БД.
    4. mark_done с file_id.
    5. На любую ошибку — mark_failed.

    Транзакция: каждый шаг (claim / mark_done / mark_failed) делает
    свой COMMIT через repo. Это специально: если процесс упадёт между
    claim и mark_done, задача останется processing — её можно подобрать
    отдельным cleanup-job (этап 14).
    """
    claimed = await task_repo.claim_task(db, task_id)
    if not claimed:
        logger.info("Task %s already claimed or not pending", task_id)
        return

    task = await task_repo.get_task(db, task_id)
    if task is None:
        # Невозможно (claim_task пройдёт только на существующей задаче),
        # но защищаемся для type-checker.
        return

    # bug_234 audit 2026-05-28: cap retry'ев. claim_task инкрементирует
    # attempts в одном UPDATE, так что после удачного claim'а task.attempts
    # уже отражает текущую попытку. Если задача уже исчерпала бюджет —
    # сразу терминальное failed, не пытаемся снова, не публикуем в outbox.
    if task.attempts > MAX_TASK_ATTEMPTS:
        logger.error(
            "Task %s exceeded max attempts (%d > %d) — marking failed",
            task_id, task.attempts, MAX_TASK_ATTEMPTS,
        )
        await task_repo.mark_failed(
            db, task_id, f"max_attempts_exceeded ({task.attempts})"
        )
        return

    try:
        if task.type == DocumentKind.CATALOG.value:
            file_id = await _handle_catalog(db, task.payload, task.created_by)
        elif task.type == DocumentKind.DIPLOMA.value:
            file_id = await _handle_diploma(db, task.payload, task.created_by)
        elif task.type == DocumentKind.DIPLOMAS_BATCH.value:
            file_id = await _handle_diplomas_batch(
                db, task.payload, task.created_by
            )
        else:
            raise ValueError(f"unknown task type: {task.type}")
    except Exception as e:  # noqa: BLE001 — последняя черта обороны воркера
        logger.exception("Task %s failed", task_id)
        # mark_failed теперь возвращает bool: False означает, что задача
        # уже не в processing (другой воркер опередил или ручной apgrade).
        # Просто логируем — в любом случае работать дальше нечего.
        if not await task_repo.mark_failed(db, task_id, str(e)):
            logger.warning(
                "Task %s no longer in processing during mark_failed",
                task_id,
            )
        return

    # bug_233 audit: mark_done теперь возвращает False, если другой
    # воркер успел перевести задачу из processing раньше. Логируем
    # warning — это сигнал о двойном consume (split-brain pool).
    if not await task_repo.mark_done(db, task_id, {"file_id": str(file_id)}):
        logger.warning(
            "Task %s no longer in processing during mark_done — "
            "possible duplicate consume; result file_id=%s not recorded",
            task_id, file_id,
        )


# ---------------------------------------------------------------------
# Хендлеры по типам
# ---------------------------------------------------------------------


async def _handle_catalog(
    db: AsyncSession,
    payload: dict,
    created_by: uuid.UUID | None,
) -> uuid.UUID:
    show_id = uuid.UUID(payload["show_id"])
    data = await document.build_catalog_data(db, show_id)
    body = pdf.render_catalog(data)
    filename = f"catalog_{show_id}.pdf"
    return await _upload_and_register(
        db, body, filename, content_type="application/pdf", created_by=created_by
    )


async def _handle_diploma(
    db: AsyncSession,
    payload: dict,
    created_by: uuid.UUID | None,
) -> uuid.UUID:
    entry_id = uuid.UUID(payload["entry_id"])
    data = await document.build_diploma_data(db, entry_id)
    body = pdf.render_diploma(data)
    filename = f"diploma_{entry_id}.pdf"
    return await _upload_and_register(
        db, body, filename, content_type="application/pdf", created_by=created_by
    )


async def _handle_diplomas_batch(
    db: AsyncSession,
    payload: dict,
    created_by: uuid.UUID | None,
) -> uuid.UUID:
    show_id = uuid.UUID(payload["show_id"])
    entry_ids = await document.list_show_entry_ids(db, show_id)
    diplomas = []
    for eid in entry_ids:
        try:
            diplomas.append(await document.build_diploma_data(db, eid))
        except ValueError as e:
            # Не валим всю пачку из-за одного "битого" entry — пропускаем.
            logger.warning("Skipping entry %s: %s", eid, e)
    body = pdf.render_diplomas_batch(diplomas)
    filename = f"diplomas_{show_id}.pdf"
    return await _upload_and_register(
        db, body, filename, content_type="application/pdf", created_by=created_by
    )


# ---------------------------------------------------------------------
# Общая часть: upload в MinIO + запись UploadedFile в БД
# ---------------------------------------------------------------------


async def _upload_and_register(
    db: AsyncSession,
    body: bytes,
    filename: str,
    *,
    content_type: str,
    created_by: uuid.UUID | None,
) -> uuid.UUID:
    """
    Загружает байты в MinIO и регистрирует UploadedFile в БД.
    Возвращает file_id для сохранения в task.result.

    Расширение всегда "pdf" — этап 8 работает только с PDF. Когда
    добавятся другие форматы (CSV экспорт, JPG ресайз), функция получит
    параметр extension.
    """
    s3_key, size_bytes = await file_storage.upload_bytes(
        body,
        content_type=content_type,
        extension="pdf",
        folder="documents",
    )
    file_obj = UploadedFile(
        uploaded_by=created_by,
        s3_key=s3_key,
        original_filename=filename,
        content_type=content_type,
        size_bytes=size_bytes,
    )
    db.add(file_obj)
    await db.commit()
    await db.refresh(file_obj)
    return file_obj.id
