"""
CSRF-защита (переход auth на httpOnly-куки, 2026-06).

Access-токен в куке браузер прикладывает к ЛЮБОМУ запросу на наш домен —
в том числе отправленному с чужого сайта (классический CSRF). Первый
рубеж — SameSite=Strict на самих куках: современный браузер вообще не
отправит их с чужого origin'а. Этот middleware — второй рубеж
(defense-in-depth): на мутирующих методах сверяем заголовок Origin со
списком разрешённых.

Запросы БЕЗ Origin пропускаем: их шлют мобильный клиент, curl,
server-to-server — у них нет автоматических кук, CSRF им не грозит.
Браузер же на cross-origin мутациях Origin ставит всегда (и "null" для
sandboxed-контекстов — строка "null" в разрешённые не попадает → 403).
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse

from app.config import settings

_MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class CSRFMiddleware:
    """Чистый ASGI (ревью 2026-10-06, BE-28): только читает заголовок Origin
    и при несовпадении отвечает 403 — BaseHTTPMiddleware тут не нужен.
    WebSocket проверяется в роутерах (dependencies.ws_origin_allowed)."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope.get("method") in _MUTATING:
            request = Request(scope)
            origin = request.headers.get("origin")
            if origin is not None and not _origin_allowed(request, origin):
                response = JSONResponse(
                    status_code=403, content={"detail": "csrf_origin_mismatch"}
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _origin_allowed(request: Request, origin: str) -> bool:
    # Same-origin запрос: Origin совпадает со scheme://host[:port] самого
    # запроса. За прокси scheme/host уже поправлены ProxyHeadersMiddleware
    # (он внешнее в стеке — выполняется раньше). Кросс-доменный фронт
    # (dev: localhost:5173 → localhost:8000) покрывается cors_allow_origins.
    own = f"{request.url.scheme}://{request.url.netloc}"
    return origin == own or origin in settings.cors_allow_origins
