"""
Починка текстовых данных, испорченных старой санитизацией (ревью 2026-10-06, BE-07).

До исправления SanitizationMiddleware прогонял каждую JSON-строку через
bleach.clean, который экранирует & < > в сущности. В БД оседали
"Tom &amp; Jerry" и "https://site.ru/?a=1&amp;b=2" (а после повторного
сохранения формы — "&amp;amp;").

Запуск:
    python -m scripts.unescape_sanitized_text            # только отчёт
    python -m scripts.unescape_sanitized_text --apply    # исправить

Что делает: проходит по всем строковым колонкам моделей (кроме колонок с
настоящим HTML и служебных), считает строки с сущностями &amp; &lt; &gt;
и с --apply раскодирует их (повторно — пока есть что раскодировать).
Перед --apply сделайте бэкап и посмотрите отчёт.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from sqlalchemy import Enum, String, Text, func, or_, select, update

from app.models import (  # noqa: F401 — регистрация всех моделей в metadata
    ad, audit, classified, consent, dog, file, kennel, litter, notification,
    outbox, post, reference, result, security_audit, show, support, task,
    upload_quota, user,
)
from app.database import async_session_factory, engine
from app.models.base import Base

logger = logging.getLogger("unescape_sanitized_text")

# Колонки, где HTML легитимен или данные не проходили санитизацию.
EXCLUDED_COLUMNS = {
    ("posts", "content"),
}
_ENTITIES = ("&amp;", "&lt;", "&gt;")
_MAX_PASSES = 5


def decode_entities_once(value: str) -> str:
    """Один проход обратного преобразования bleach (& — последним)."""
    return value.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _text_columns():
    for table in Base.metadata.tables.values():
        for col in table.columns:
            if not isinstance(col.type, (String, Text)) or isinstance(col.type, Enum):
                continue
            if (table.name, col.name) in EXCLUDED_COLUMNS:
                continue
            yield table, col


def _has_entities(col):
    return or_(*(col.contains(e) for e in _ENTITIES))


def _decode_sql(col):
    return func.replace(
        func.replace(func.replace(col, "&lt;", "<"), "&gt;", ">"), "&amp;", "&"
    )


async def run(apply: bool) -> int:
    total = 0
    async with async_session_factory() as db:
        for table, col in _text_columns():
            count = (
                await db.execute(
                    select(func.count()).select_from(table).where(_has_entities(col))
                )
            ).scalar_one()
            if not count:
                continue
            total += count
            logger.info("%s.%s: %d строк с сущностями", table.name, col.name, count)
            if apply:
                for _ in range(_MAX_PASSES):
                    res = await db.execute(
                        update(table)
                        .where(_has_entities(col))
                        .values({col.name: _decode_sql(col)})
                    )
                    if not getattr(res, "rowcount", 0):
                        break
        if apply:
            await db.commit()
    logger.info("Итого строк: %d (%s)", total, "исправлено" if apply else "dry-run")
    return total


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="записать изменения")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        await run(args.apply)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
