# app/utils/docx_render.py
"""
Рендер официальных документов РКФ из DOCX-шаблонов.

docxtpl подставляет данные в .docx-шаблон → bytes. Документы РКФ имеют
сложное фиксированное оформление (рамки, шрифты, двуязычные блоки) — шаблон
в Word сохраняет его 1-в-1, бэкенд лишь подставляет данные.

Вывод только .docx. PDF не делаем намеренно: точная конвертация .docx→PDF
требует офисного движка (LibreOffice/Word), тащить тяжёлую зависимость не
хотим. Готовый .docx при необходимости сохраняется в PDF из Word вручную.
"""

from __future__ import annotations

import io
from pathlib import Path

from docxtpl import DocxTemplate

# app/utils/docx_render.py -> app/ -> app/templates/documents
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates" / "documents"

DOCX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


def render_docx(template_name: str, context: dict) -> bytes:
    """
    Подставляет context в шаблон templates/documents/<template_name>
    и возвращает байты готового .docx.
    """
    tpl = DocxTemplate(str(TEMPLATES_DIR / template_name))
    tpl.render(context)
    buf = io.BytesIO()
    tpl.save(buf)
    return buf.getvalue()
