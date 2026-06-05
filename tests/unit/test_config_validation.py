"""Unit: Settings отвергает небезопасный SECRET_KEY (аудит C1)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings

_DB = "postgresql+asyncpg://u:p@localhost:5432/db"
_STRONG = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


def test_empty_secret_rejected_in_prod():
    with pytest.raises(ValidationError):
        Settings(database_url=_DB, secret_key="", debug=False)


def test_placeholder_secret_rejected_in_prod():
    with pytest.raises(ValidationError):
        Settings(database_url=_DB, secret_key="change-me-in-production", debug=False)


def test_short_secret_rejected_in_prod():
    with pytest.raises(ValidationError):
        Settings(database_url=_DB, secret_key="too-short", debug=False)


def test_strong_secret_accepted_in_prod():
    s = Settings(database_url=_DB, secret_key=_STRONG, debug=False)
    assert s.secret_key == _STRONG


def test_weak_secret_allowed_in_debug():
    # В dev допускаем (громкий warning), чтобы локальный стек не падал.
    s = Settings(database_url=_DB, secret_key="", debug=True)
    assert s.debug is True
