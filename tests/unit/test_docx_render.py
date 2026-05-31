# tests/unit/test_docx_render.py
import pytest

from app.utils import docx_render


def test_docx_content_type_is_wordprocessing():
    assert docx_render.DOCX_CONTENT_TYPE.endswith("wordprocessingml.document")


def test_render_docx_produces_valid_docx():
    # render_docx читает шаблон из TEMPLATES_DIR; пустого контекста достаточно
    # (отсутствующие плейсхолдеры рендерятся в '' — jinja default Undefined).
    if not (docx_render.TEMPLATES_DIR / "diploma.docx").exists():
        pytest.skip("diploma.docx отсутствует")
    body = docx_render.render_docx("diploma.docx", {})
    assert body[:2] == b"PK"
    assert len(body) > 2000
