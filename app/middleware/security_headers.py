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
        return response
