import html
import json
from email.message import Message
from typing import Any
import nh3
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

# ИСПРАВЛЕНО: bleach менял значения чувствительных полей (пароль "ab<x>cd"
# превращался в "abcd"), из-за чего хеш при регистрации не совпадал с хешем
# при логине. Перечисленные поля передаются как есть.
SENSITIVE_FIELDS = frozenset(
    {
        "password",
        "current_password",
        "new_password",
        "old_password",
        "token",
        "access_token",
        "refresh_token",
        "hashed_password",
        "api_key",
        "secret",
    }
)

# Этап 17 (блог): поля с осмысленным HTML, который глобальный bleach
# (tags=[], strip=True) вырезал бы целиком. Для них санитизация делается
# СВОИМ allowlist'ом в сервисе (app/utils/html_sanitize.py).
#
# Аудит L3: раньше «content» пропускался ГЛОБАЛЬНО по имени поля — любой
# будущий эндпоинт с полем content молча получил бы сырой ввод. Теперь
# passthrough привязан к маршруту (write-ручки блога, _is_raw_html_route):
# вне него content чистится как обычный текст.
RAW_HTML_FIELDS = frozenset({"content"})


def _is_json_content_type(content_type: str) -> bool:
    """Тот же разбор, что у FastAPI при чтении JSON-тела.

    ИСПРАВЛЕНО (ревью безопасности 2026-10-03, #10): раньше проверяли
    startswith("application/json"), а FastAPI разбирает как JSON ещё и
    "application/<что угодно>+json" и заголовок в любом регистре. Такой
    запрос проходил мимо санитизации, и в БД попадал сырой HTML.
    """
    if not content_type:
        return False
    message = Message()
    message["content-type"] = content_type
    if message.get_content_maintype() != "application":
        return False
    subtype = message.get_content_subtype()
    return subtype == "json" or subtype.endswith("+json")


def _is_raw_html_route(request: Request) -> bool:
    """True для write-ручек блога (POST/PUT/PATCH /posts[/...]). Только на них
    поле content передаётся сырым (его чистит сервис своим allowlist'ом)."""
    if request.method not in ("POST", "PUT", "PATCH"):
        return False
    path = request.url.path.rstrip("/")
    return path == "/posts" or path.startswith("/posts/")


_MAX_CLEAN_PASSES = 5


def _strip_tags(text: str) -> str:
    """
    Удалить HTML-теги, НЕ экранируя текст.

    ИСПРАВЛЕНО (ревью 2026-10-06, BE-07): очистка HTML (раньше bleach, с
    BE-29 — nh3) не только вырезает
    теги, но и экранирует & < > в сущности. В БД оседали
    "https://site.ru/?a=1&amp;b=2" и "Tom &amp; Jerry", а React выводил
    их буквально. Защита от XSS — экранирование при ВЫВОДЕ (React, Jinja
    autoescape), здесь лишь убираем разметку.

    Поэтому после очистки раскодируем сущности обратно. Повторяем до
    неподвижной точки: иначе «&lt;script&gt;» после раскодирования
    превратился бы в настоящий тег.
    """
    current = text
    for _ in range(_MAX_CLEAN_PASSES):
        cleaned = html.unescape(nh3.clean(current, tags=set()))
        if cleaned == current:
            return cleaned
        current = cleaned
    return current


def _sanitize(
    value: Any,
    *,
    key: str | None = None,
    raw_fields: frozenset[str] = frozenset(),
) -> Any:
    # ИСПРАВЛЕНО: пропускаем чувствительные поля без модификации,
    # иначе ломается аутентификация и логика проверки токенов.
    # raw_fields (route-scoped, см. _is_raw_html_route) пропускаем по той же
    # механике — их чистит сервис своим allowlist-bleach. По умолчанию пусто
    # → content санитизируется как обычное поле.
    if key is not None and (key in SENSITIVE_FIELDS or key in raw_fields):
        return value
    if isinstance(value, str):
        return _strip_tags(value)
    if isinstance(value, dict):
        return {
            k: _sanitize(v, key=k, raw_fields=raw_fields)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_sanitize(v, key=key, raw_fields=raw_fields) for v in value]
    return value


class SanitizationMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        content_type = request.headers.get("content-type", "")
        if not _is_json_content_type(content_type):
            return await call_next(request)

        body = await request.body()
        if not body:
            return await call_next(request)

        try:
            data = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse(
                {"detail": "Invalid JSON"}, status_code=400
            )

        # content пропускаем сырым ТОЛЬКО на write-ручках блога (L3).
        raw_fields = (
            RAW_HTML_FIELDS if _is_raw_html_route(request) else frozenset()
        )
        sanitized = _sanitize(data, raw_fields=raw_fields)
        new_body = json.dumps(sanitized).encode("utf-8")

        # ИСПРАВЛЕНО (bug_001 ultrareview): подмены только `_receive`
        # недостаточно. BaseHTTPMiddleware оборачивает request в
        # _CachedRequest, чей `wrapped_receive` сначала проверяет
        # `self._body` и возвращает кэш (заполненный нашим
        # `await request.body()` выше), и только при отсутствии кэша
        # обращается к `_receive`. Поэтому downstream получал ОРИГИНАЛЬНОЕ
        # тело, а bleach.clean был молчаливым no-op.
        # Перезаписываем _body — этим починили санитизацию вообще; replace
        # _receive оставляем «belt-and-braces» для случая, если кто-то
        # downstream вручную сбросит _body.
        request._body = new_body  # type: ignore[attr-defined]

        async def receive():
            return {"type": "http.request", "body": new_body, "more_body": False}

        request._receive = receive
        return await call_next(request)
