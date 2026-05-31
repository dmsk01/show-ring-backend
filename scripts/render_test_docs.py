r"""
Проверка выдачи официальных документов на реальных данных БД (этап 8),
в обход очереди/воркера: билдер контекста → docx_render.render_docx → .docx.

Запуск (после scripts.seed_test_show):
    .\venv\Scripts\python.exe -m scripts.render_test_docs

Кладёт catalog.docx / diploma.docx / ring_sheet.docx в output_docs/.
PDF здесь не делаем — для него нужен LibreOffice (soffice); если он
установлен, конвертацию делает воркер при format=pdf.
"""

from __future__ import annotations

import asyncio
import io
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.database import async_session_factory, engine
from app.models import (  # noqa: F401  — регистрация моделей
    ad, audit, classified, dog, file, kennel, litter,
    notification, outbox, reference, result, show, support, task,
)
from app.models.show import Show, ShowEntry
from app.models.user import User
from app.services.document_official import (
    build_catalog_context,
    build_diploma_context,
    build_ring_sheets_context,
)
from app.utils import docx_render
from scripts.seed_test_show import ORG_EMAIL, SHOW_NAME

OUT = Path(__file__).resolve().parent.parent / "output_docs"


def _check(name: str, data: bytes) -> None:
    xml = zipfile.ZipFile(io.BytesIO(data)).read("word/document.xml").decode("utf-8")
    leftover = "{{" in xml or "{%" in xml
    (OUT / name).write_bytes(data)
    print(f"  {name}: {len(data):>7} байт | tbl={xml.count('<w:tbl>')} | "
          f"leftover_jinja={leftover}")


async def main() -> None:
    OUT.mkdir(exist_ok=True)
    try:
        async with async_session_factory() as db:
            org = (
                await db.execute(select(User).where(User.email == ORG_EMAIL))
            ).scalar_one()
            show = (
                await db.execute(
                    select(Show).where(
                        Show.organizer_id == org.id, Show.name == SHOW_NAME
                    )
                )
            ).scalar_one()
            entry = (
                await db.execute(
                    select(ShowEntry).where(ShowEntry.show_id == show.id)
                    .order_by(ShowEntry.catalog_number).limit(1)
                )
            ).scalar_one()

            print(f"show_id={show.id}  entry_id={entry.id}")
            print("Рендер документов ->", OUT)

            cat = await build_catalog_context(db, show.id)
            _check("catalog.docx", docx_render.render_docx("catalog.docx", cat))

            dip = await build_diploma_context(db, entry.id)
            _check("diploma.docx", docx_render.render_docx("diploma.docx", dip))

            rs = await build_ring_sheets_context(db, show.id)
            _check("ring_sheet.docx", docx_render.render_docx("ring_sheet.docx", rs))

            print(f"\nКонтекст каталога: групп={len(cat['groups'])}, "
                  f"всего записей={cat['total_entries']}; "
                  f"ведомостей={len(rs['sheets'])}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
