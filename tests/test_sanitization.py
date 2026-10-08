"""Sanitization middleware — критично: чувствительные поля НЕ трогаются.

Без этого инварианта bleach молча изменяет пароли ('ab<x>cd' → 'abcd'),
и пользователь после регистрации не может войти.
"""

from app.middleware.sanitization import (
    SENSITIVE_FIELDS,
    _is_raw_html_route,
    _sanitize,
)


class _FakeRequest:
    """Минимальный заменитель Request: только .method и .url.path."""

    def __init__(self, method: str, path: str):
        self.method = method
        self.url = type("_U", (), {"path": path})()


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


# ---------------------------------------------------------------------
# raw-HTML passthrough scoped to blog write routes (аудит L3)
# ---------------------------------------------------------------------


def test_content_sanitized_by_default():
    """Вне blog-ручек поле content чистится как обычный текст (XSS-защита)."""
    cleaned = _sanitize({"content": "<script>x</script><p>ok</p>"})
    assert "<script>" not in cleaned["content"]


def test_content_passthrough_only_with_raw_fields():
    """С raw_fields={'content'} (blog-ручка) content проходит как есть —
    чистит уже сервис своим allowlist'ом."""
    cleaned = _sanitize(
        {"content": "<p>ok</p>"}, raw_fields=frozenset({"content"})
    )
    assert cleaned["content"] == "<p>ok</p>"


def test_sensitive_preserved_without_raw_fields():
    """Чувствительные поля не зависят от raw_fields — всегда as-is."""
    cleaned = _sanitize({"password": "a<b>c"})
    assert cleaned["password"] == "a<b>c"


def test_raw_html_route_only_blog_writes():
    assert _is_raw_html_route(_FakeRequest("POST", "/posts"))
    assert _is_raw_html_route(_FakeRequest("PUT", "/posts/abc-123"))
    # GET и другие домены — нет.
    assert not _is_raw_html_route(_FakeRequest("GET", "/posts"))
    assert not _is_raw_html_route(_FakeRequest("POST", "/classifieds"))
    assert not _is_raw_html_route(_FakeRequest("POST", "/posts-other"))


def test_json_content_type_detection_matches_fastapi():
    """Middleware должен чистить всё, что FastAPI разберёт как JSON.

    Раньше проверка была startswith("application/json"), а FastAPI парсит
    ещё и "+json"-подтипы и заголовок в любом регистре — такие запросы
    проходили мимо санитизации.
    """
    from app.middleware.sanitization import _is_json_content_type

    assert _is_json_content_type("application/json")
    assert _is_json_content_type("application/json; charset=utf-8")
    assert _is_json_content_type("Application/JSON")
    assert _is_json_content_type("application/merge-patch+json")
    assert _is_json_content_type("application/vnd.api+json")
    assert not _is_json_content_type("")
    assert not _is_json_content_type("multipart/form-data; boundary=x")
    assert not _is_json_content_type("text/plain")


# ---------------------------------------------------------------------
# BE-07 (ревью 2026-10-06): санитизация не должна HTML-экранировать текст
# ---------------------------------------------------------------------


def test_ampersand_is_not_escaped():
    cleaned = _sanitize({"name": "Tom & Jerry"})
    assert cleaned["name"] == "Tom & Jerry"


def test_url_query_is_preserved():
    url = "https://site.ru/page?a=1&b=2"
    assert _sanitize({"website": url})["website"] == url


def test_less_than_sign_is_preserved():
    assert _sanitize({"description": "щенки <3 мес, a < b"})["description"] == (
        "щенки <3 мес, a < b"
    )


def test_tags_are_still_stripped():
    assert _sanitize({"name": "<b>Рекс</b>"})["name"] == "Рекс"


def test_entity_encoded_tags_do_not_survive():
    # После «раскодирования» сущностей тег не должен появиться в данных.
    cleaned = _sanitize({"name": "&lt;script&gt;alert(1)&lt;/script&gt;x"})
    assert "<script" not in cleaned["name"].lower()


def test_repair_script_decodes_bleach_entities():
    from scripts.unescape_sanitized_text import decode_entities_once

    assert decode_entities_once("Tom &amp; Jerry") == "Tom & Jerry"
    assert decode_entities_once("a &lt;3 &gt; b") == "a <3 > b"
    # Двойное экранирование снимается за два прохода, а не одним «перескоком».
    assert decode_entities_once("&amp;amp;") == "&amp;"
