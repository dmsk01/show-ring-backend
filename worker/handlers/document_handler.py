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

import asyncio
import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.file import UploadedFile
from app.models.task import TaskStatusEnum
from app.repositories import task as task_repo
from app.schemas.task import DocumentKind
from app.services import document, document_official, file_storage
from app.utils import docx_render, pdf

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
        elif task.type == DocumentKind.CATALOG_OFFICIAL.value:
            file_id = await _handle_catalog_official(
                db, task.payload, task.created_by
            )
        elif task.type == DocumentKind.DIPLOMA_OFFICIAL.value:
            file_id = await _handle_diploma_official(
                db, task.payload, task.created_by
            )
        elif task.type == DocumentKind.DIPLOMAS_BATCH_OFFICIAL.value:
            file_id = await _handle_diplomas_batch_official(
                db, task.payload, task.created_by
            )
        elif task.type == DocumentKind.RING_SHEETS_OFFICIAL.value:
            file_id = await _handle_ring_sheets_official(
                db, task.payload, task.created_by
            )
        elif task.type == DocumentKind.CERTIFICATES_OFFICIAL.value:
            file_id = await _handle_certificates_official(
                db, task.payload, task.created_by
            )
        else:
            raise ValueError(f"unknown task type: {task.type}")
    except Exception as e:  # noqa: BLE001 — последняя черта обороны воркера
        logger.exception("Task %s failed", task_id)
        # ИСПРАВЛЕНО (review 2026-05-28): _upload_and_register больше не
        # коммитит UploadedFile сразу — он сидит в pending-стейте сессии.
        # При исключении откатываем pending INSERT, иначе следующий
        # mark_failed (UPDATE + commit) затянул бы файл-сироту в БД,
        # ссылающийся на ничего в S3 (если падение случилось ДО upload'а).
        await db.rollback()
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
    extension: str = "pdf",
) -> uuid.UUID:
    """
    Загружает байты в MinIO и регистрирует UploadedFile в БД.
    Возвращает file_id для сохранения в task.result.

    extension по умолчанию "pdf" (этап 8). Официальные документы (этап
    docs) передают "docx" или "pdf" в зависимости от запрошенного формата.

    ИСПРАВЛЕНО (review 2026-05-28): убрали внутренний commit/refresh.
    Раньше последовательность была: upload_bytes → INSERT UploadedFile
    + commit → mark_done (отдельный commit). Если процесс убивали между
    двумя commit'ами, UploadedFile оставался в БД, а task — в processing.
    Scheduler через час re-публиковал задачу → новый upload → дубликат
    UploadedFile, первый сиротеет.
    Сейчас UploadedFile только flush'ится (id уже назначен default'ом),
    а commit делает один вызов mark_done в process_document_task —
    UPDATE Task и INSERT UploadedFile попадают в одну транзакцию. При
    крэше до mark_done вся работа откатывается: задача возвращается в
    pending через cleanup-cron (см. requeue_stuck_tasks).
    """
    s3_key, size_bytes = await file_storage.upload_bytes(
        body,
        content_type=content_type,
        extension=extension,
        folder="documents",
    )
    file_obj = UploadedFile(
        uploaded_by=created_by,
        s3_key=s3_key,
        original_filename=filename,
        content_type=content_type,
        size_bytes=size_bytes,
        # Документы содержат ПДн (ФИО, чип, клеймо) — НЕ публичны.
        # Доступ только через /tasks/{id}/download (ACL автор/admin);
        # публичный GET /files/{id} приватные файлы не отдаёт.
        is_public=False,
    )
    db.add(file_obj)
    # flush — заставляет SQLAlchemy выполнить INSERT и применить
    # default=uuid.uuid4 (id назначается в Python до INSERT, см.
    # UploadedFile-модель). refresh не делаем — created_at нам тут не
    # нужен, а лишний SELECT не хочется. commit будет в mark_done.
    await db.flush()
    return file_obj.id


# ---------------------------------------------------------------------
# Официальные документы РКФ (DOCX-шаблоны)
# ---------------------------------------------------------------------


async def _render_official(
    template_name: str, context: dict, basename: str
) -> tuple[bytes, str, str, str]:
    """
    Рендерит .docx из шаблона. Блокирующий docxtpl-рендер уводим в
    отдельный поток, чтобы не вешать event loop воркера. Вывод только
    .docx (PDF не делаем — см. app/utils/docx_render.py).

    Возвращает (body, extension, content_type, filename).
    """
    docx_bytes = await asyncio.to_thread(
        docx_render.render_docx, template_name, context
    )
    return (
        docx_bytes,
        "docx",
        docx_render.DOCX_CONTENT_TYPE,
        f"{basename}.docx",
    )


async def _handle_catalog_official(
    db: AsyncSession, payload: dict, created_by: uuid.UUID | None
) -> uuid.UUID:
    show_id = uuid.UUID(payload["show_id"])
    ctx = await document_official.build_catalog_context(db, show_id)
    body, ext, ctype, filename = await _render_official(
        "catalog.docx", ctx, f"catalog_official_{show_id}"
    )
    return await _upload_and_register(
        db, body, filename, content_type=ctype, created_by=created_by,
        extension=ext,
    )


async def _handle_diploma_official(
    db: AsyncSession, payload: dict, created_by: uuid.UUID | None
) -> uuid.UUID:
    entry_id = uuid.UUID(payload["entry_id"])
    ctx = await document_official.build_diploma_context(db, entry_id)
    body, ext, ctype, filename = await _render_official(
        "diploma.docx", ctx, f"diploma_official_{entry_id}"
    )
    return await _upload_and_register(
        db, body, filename, content_type=ctype, created_by=created_by,
        extension=ext,
    )


async def _handle_diplomas_batch_official(
    db: AsyncSession, payload: dict, created_by: uuid.UUID | None
) -> uuid.UUID:
    show_id = uuid.UUID(payload["show_id"])
    ctx = await document_official.build_diplomas_batch_context(db, show_id)
    body, ext, ctype, filename = await _render_official(
        "diplomas_batch.docx", ctx, f"diplomas_official_{show_id}"
    )
    return await _upload_and_register(
        db, body, filename, content_type=ctype, created_by=created_by,
        extension=ext,
    )


async def _handle_ring_sheets_official(
    db: AsyncSession, payload: dict, created_by: uuid.UUID | None
) -> uuid.UUID:
    show_id = uuid.UUID(payload["show_id"])
    ring_id = payload.get("ring_id")
    ring_uuid = uuid.UUID(ring_id) if ring_id else None
    ctx = await document_official.build_ring_sheets_context(
        db, show_id, ring_uuid
    )
    body, ext, ctype, filename = await _render_official(
        "ring_sheet.docx", ctx, f"ring_sheets_{show_id}"
    )
    return await _upload_and_register(
        db, body, filename, content_type=ctype, created_by=created_by,
        extension=ext,
    )


async def _handle_certificates_official(
    db: AsyncSession, payload: dict, created_by: uuid.UUID | None
) -> uuid.UUID:
    show_id = uuid.UUID(payload["show_id"])
    entry_id = payload.get("entry_id")
    entry_uuid = uuid.UUID(entry_id) if entry_id else None
    ctx = await document_official.build_certificates_context(
        db, show_id, entry_uuid
    )
    body, ext, ctype, filename = await _render_official(
        "certificate.docx", ctx, f"certificates_{show_id}"
    )
    return await _upload_and_register(
        db, body, filename, content_type=ctype, created_by=created_by,
        extension=ext,
    )
