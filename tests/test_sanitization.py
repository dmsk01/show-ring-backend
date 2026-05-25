"""Sanitization middleware — критично: чувствительные поля НЕ трогаются.

Без этого инварианта bleach молча изменяет пароли ('ab<x>cd' → 'abcd'),
и пользователь после регистрации не может войти.
"""

from app.middleware.sanitization import SENSITIVE_FIELDS, _sanitize


def test_password_is_not_sanitized():
    """Пароль с HTML-подобными символами должен дойти до Pydantic как есть."""
    payload = {"email": "a@b.c", "password": "ab<x>cd&e"}
    cleaned = _sanitize(payload)
    assert cleaned["password"] == "ab<x>cd&e"


def test_refresh_token_is_not_sanitized():
    """Refresh-токен (base64/hex) не должен переписываться bleach'ом."""
    raw = "abc<def>123&xyz"
    cleaned = _sanitize({"refresh_token": raw})
    assert cleaned["refresh_token"] == raw


def test_regular_string_field_is_sanitized():
    """Обычные текстовые поля (например, имя/описание) санитизируются — XSS-защита."""
    cleaned = _sanitize({"name": "<script>alert(1)</script>bob"})
    # bleach.clean(tags=[], strip=True) убирает теги, оставляет текст
    assert "<script>" not in cleaned["name"]
    assert "bob" in cleaned["name"]


def test_nested_sensitive_field_in_dict_is_preserved():
    """Чувствительные ключи на любом уровне вложенности не трогаются."""
    cleaned = _sanitize({"creds": {"password": "x<y>z", "name": "<i>n</i>"}})
    assert cleaned["creds"]["password"] == "x<y>z"
    assert "<i>" not in cleaned["creds"]["name"]


def test_sensitive_fields_listed():
    """Smoke-test: список чувствительных полей покрывает базовый набор."""
    must_have = {"password", "refresh_token", "access_token", "token", "api_key"}
    assert must_have.issubset(SENSITIVE_FIELDS)
