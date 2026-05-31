"""
Воркер-обработка изображений (этап 8): генерация вариантов (превью + средний
с водяным знаком) загруженного файла.

Задача `process_image`, payload `{"file_id": ...}`. Поток: claim задачи →
скачиваем оригинал из MinIO → Pillow-варианты (в отдельном потоке, CPU-bound)
→ upload в MinIO + строки file_variants → mark_done(result={"variants": [...]}).
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.file import FileVariant, UploadedFile
from app.repositories import task as task_repo
from app.services import file_storage
from app.utils.image_processing import VARIANTS, make_variant

logger = logging.getLogger(__name__)

MAX_TASK_ATTEMPTS = 5


async def process_image_task(db: AsyncSession, task_id: uuid.UUID) -> None:
    """Точка входа: claim → генерация вариантов → done/failed."""
    claimed = await task_repo.claim_task(db, task_id)
    if not claimed:
        logger.info("Image task %s already claimed or not pending", task_id)
        return
    task = await task_repo.get_task(db, task_id)
    if task is None:
        return
    if task.attempts > MAX_TASK_ATTEMPTS:
        logger.error("Image task %s exceeded max attempts", task_id)
        await task_repo.mark_failed(
            db, task_id, f"max_attempts_exceeded ({task.attempts})"
        )
        return
    try:
        file_id = uuid.UUID(task.payload["file_id"])
        variant_ids = await _generate_variants(db, file_id)
    except Exception as e:  # noqa: BLE001 — последняя черта обороны воркера
        logger.exception("Image task %s failed", task_id)
        await db.rollback()
        await task_repo.mark_failed(db, task_id, str(e)[:500])
        return
    await task_repo.mark_done(db, task_id, {"variants": variant_ids})


async def _generate_variants(
    db: AsyncSession, file_id: uuid.UUID
) -> list[str]:
    src = await db.get(UploadedFile, file_id)
    if src is None:
        raise ValueError("file_not_found")

    # Идемпотентность: сносим прежние варианты (БД + MinIO), чтобы повтор
    # задачи не плодил дубли.
    existing = (
        await db.execute(
            select(FileVariant).where(FileVariant.file_id == file_id)
        )
    ).scalars().all()
    for v in existing:
        try:
            await file_storage.delete_file(v.s3_key)
        except Exception:  # noqa: BLE001
            logger.warning("variant cleanup: delete %s failed", v.s3_key)
        await db.delete(v)
    await db.flush()

    original, _ctype = await file_storage.get_file_stream(src.s3_key)

    out_ids: list[str] = []
    for kind, max_size, watermark in VARIANTS:
        # Pillow — CPU-bound, уводим в поток, чтобы не вешать event loop.
        jpeg, width, height = await asyncio.to_thread(
            make_variant, original, max_size, watermark
        )
        s3_key, size = await file_storage.upload_bytes(
            jpeg, content_type="image/jpeg", extension="jpg", folder="variants"
        )
        variant = FileVariant(
            file_id=file_id, kind=kind, s3_key=s3_key,
            content_type="image/jpeg", width=width, height=height,
            has_watermark=watermark, size_bytes=size,
        )
        db.add(variant)
        await db.flush()
        out_ids.append(str(variant.id))
    await db.commit()
    return out_ids
