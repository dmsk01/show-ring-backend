"""
Перегенерация вариантов изображений (превью и medium с водяным знаком).

Когда нужен:
- после смены водяного знака (WATERMARK_TEXT в app/utils/image_processing.py)
  — старые варианты нарисованы с прежним текстом;
- для фото, у которых вариантов нет (загружены до появления воркера или
  обработка упала): с BE-24 такие фото посторонним не показываются вовсе.

Ставит задачи в очередь image_task через outbox — обрабатывает их
worker-files, так что запускать можно на работающем стеке:

    dc exec api python -m scripts.regenerate_image_variants --missing-only  # только без вариантов
    dc exec api python -m scripts.regenerate_image_variants                 # все фото (смена знака)

--dry-run — только посчитать.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import exists, select  # noqa: E402

from app.database import async_session_factory, engine  # noqa: E402
from app.models.file import FileVariant, UploadedFile  # noqa: E402
from app.services.task_dispatch import create_and_enqueue_task  # noqa: E402
from app.services.task_queues import IMAGE_TASK_TYPE  # noqa: E402

logger = logging.getLogger("regenerate_image_variants")


async def run(*, missing_only: bool, dry_run: bool) -> int:
    query = select(UploadedFile.id).where(
        UploadedFile.is_public.is_(True),
        UploadedFile.content_type.like("image/%"),
    )
    if missing_only:
        query = query.where(
            ~exists().where(
                FileVariant.file_id == UploadedFile.id,
                FileVariant.kind == "medium",
            )
        )
    async with async_session_factory() as db:
        ids = (await db.execute(query.order_by(UploadedFile.created_at))).scalars().all()
        logger.info("изображений к обработке: %d", len(ids))
        if dry_run:
            return len(ids)
        for n, file_id in enumerate(ids, 1):
            # Задача + outbox-событие в одной транзакции (коммитит сама).
            await create_and_enqueue_task(
                db, type_=IMAGE_TASK_TYPE, payload={"file_id": str(file_id)},
                created_by=None,
            )
            if n % 100 == 0:
                logger.info("поставлено в очередь: %d/%d", n, len(ids))
    logger.info("готово: %d задач в очереди image_task", len(ids))
    return len(ids)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(
        description="Перегенерация вариантов изображений (превью и medium со знаком)."
    )
    parser.add_argument("--missing-only", action="store_true",
                        help="только фото без варианта medium")
    parser.add_argument("--dry-run", action="store_true", help="только посчитать")
    args = parser.parse_args()
    try:
        await run(missing_only=args.missing_only, dry_run=args.dry_run)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
