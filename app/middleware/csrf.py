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

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.config import settings

_MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class CSRFMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        if request.method in _MUTATING:
            origin = request.headers.get("origin")
            if origin is not None and not _origin_allowed(request, origin):
                # HTTPException тут не годится: exception handlers FastAPI
                # живут ВНУТРИ middleware-стека и наш 403 превратился бы
                # в 500. Отвечаем готовым JSONResponse.
                return JSONResponse(
                    status_code=403,
                    content={"detail": "csrf_origin_mismatch"},
                )
        return await call_next(request)


def _origin_allowed(request: Request, origin: str) -> bool:
    # Same-origin запрос: Origin совпадает со scheme://host[:port] самого
    # запроса. За прокси scheme/host уже поправлены ProxyHeadersMiddleware
    # (он внешнее в стеке — выполняется раньше). Кросс-доменный фронт
    # (dev: localhost:5173 → localhost:8000) покрывается cors_allow_origins.
    own = f"{request.url.scheme}://{request.url.netloc}"
    return origin == own or origin in settings.cors_allow_origins
