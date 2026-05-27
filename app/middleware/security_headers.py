"""
Security headers middleware (этап 14).

Добавляет к каждому ответу набор защитных заголовков:
- X-Content-Type-Options: nosniff   — браузер не угадывает MIME, исключает
  ситуацию, когда text/html отрендерится из ответа image/jpeg.
- X-Frame-Options: DENY             — нельзя встроить наш API в iframe
  (защита от clickjacking).
- Referrer-Policy: strict-origin-when-cross-origin — не утекаем полный
  URL в Referer на сторонние домены.
- Permissions-Policy                — отключаем доступ к камере/микрофону
  для контента, отдаваемого API (микро-меры — API не должно их
  запрашивать вообще, но prophylaxис не помешает).

Strict-Transport-Security НЕ добавляем здесь: HSTS должен ставить
TLS-терминирующий nginx/Caddy, у которого больше контекста (домен,
preload список и т.д.).
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.config import settings


# CSP для JSON-API: ничего не разрешаем, API не отдаёт HTML.
# frame-ancestors 'none' дублирует X-Frame-Options для современных
# браузеров (XFO считается устаревшим в пользу CSP).
_CSP_API = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        # setdefault на mutableHeaders нет — пишем напрямую. Если уже
        # установлено выше (например, обработчиком), не перезаписываем.
        h = response.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault(
            "Referrer-Policy", "strict-origin-when-cross-origin"
        )
        h.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=()",
        )
        # HSTS — только когда явно включён в конфиге и запрос пришёл
        # по HTTPS. Иначе подсказывали бы браузеру переходить на HTTPS
        # для домена, который пока работает только на HTTP — пользователи
        # получали бы ERR_SSL_PROTOCOL_ERROR.
        if settings.hsts_enabled and request.url.scheme == "https":
            h.setdefault(
                "Strict-Transport-Security",
                f"max-age={settings.hsts_max_age_seconds}; includeSubDomains",
            )
        if settings.csp_enabled:
            h.setdefault("Content-Security-Policy", _CSP_API)
        return response
