"""Unit: Swagger/OpenAPI не публикуются в проде.

Полная карта API (включая admin- и внутренние ручки) не должна быть
доступна анонимно через /api/docs и /api/openapi.json.
"""
from __future__ import annotations

from app.main import _docs_settings


def test_docs_disabled_when_not_debug():
    assert _docs_settings(debug=False) == {
        "docs_url": None,
        "redoc_url": None,
        "openapi_url": None,
    }


def test_docs_enabled_in_debug():
    urls = _docs_settings(debug=True)
    assert urls["docs_url"] == "/docs"
    assert urls["openapi_url"] == "/openapi.json"
