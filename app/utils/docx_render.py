# app/utils/docx_render.py
"""
Рендер официальных документов из DOCX-шаблонов и конвертация в PDF.

Поток: docxtpl подставляет данные в .docx-шаблон → bytes. Если нужен PDF —
конвертируем тот же .docx через LibreOffice headless (soffice).

Почему LibreOffice, а не сборка PDF в коде: документы РКФ имеют сложное
фиксированное оформление (рамки, шрифты, двуязычные блоки). Шаблон в Word
сохраняет его 1-в-1; повторять это программно — дорого и неточно.

soffice — блокирующий subprocess. В асинхронном воркере вызывать через
asyncio.to_thread (см. document_handler).
"""

from __future__ import annotations

import io
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from docxtpl import DocxTemplate

logger = logging.getLogger(__name__)

# app/utils/docx_render.py -> app/ -> app/templates/documents
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates" / "documents"

DOCX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


class PdfConversionError(RuntimeError):
    """LibreOffice недоступен или конвертация завершилась ошибкой."""


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


def _find_soffice() -> str | None:
    """Путь к LibreOffice/soffice или None, если не найден."""
    for name in ("soffice", "libreoffice"):
        path = shutil.which(name)
        if path:
            return path
    # Типовой путь установки на Windows.
    win = Path(r"C:/Program Files/LibreOffice/program/soffice.exe")
    if win.exists():
        return str(win)
    return None


def convert_docx_to_pdf(docx_bytes: bytes, *, timeout: int = 120) -> bytes:
    """
    Конвертирует .docx (байты) в PDF (байты) через LibreOffice headless.
    Бросает PdfConversionError, если soffice не найден/упал.
    """
    soffice = _find_soffice()
    if soffice is None:
        raise PdfConversionError("LibreOffice (soffice) not found on PATH")
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        src = tmpdir / "in.docx"
        src.write_bytes(docx_bytes)
        try:
            proc = subprocess.run(
                [
                    soffice,
                    "--headless",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    str(tmpdir),
                    str(src),
                ],
                capture_output=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            raise PdfConversionError(f"soffice timeout after {timeout}s") from e
        out = tmpdir / "in.pdf"
        if proc.returncode != 0 or not out.exists():
            err = proc.stderr.decode(errors="ignore")[:500]
            raise PdfConversionError(
                f"soffice failed: rc={proc.returncode} err={err}"
            )
        return out.read_bytes()
