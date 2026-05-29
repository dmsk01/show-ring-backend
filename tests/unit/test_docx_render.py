# tests/unit/test_docx_render.py
import builtins
import pytest

from app.utils import docx_render


def test_find_soffice_returns_none_when_absent(monkeypatch):
    monkeypatch.setattr(docx_render.shutil, "which", lambda name: None)
    monkeypatch.setattr(docx_render.Path, "exists", lambda self: False)
    assert docx_render._find_soffice() is None


def test_convert_raises_when_soffice_missing(monkeypatch):
    monkeypatch.setattr(docx_render, "_find_soffice", lambda: None)
    with pytest.raises(docx_render.PdfConversionError):
        docx_render.convert_docx_to_pdf(b"PK\x03\x04 fake docx")
