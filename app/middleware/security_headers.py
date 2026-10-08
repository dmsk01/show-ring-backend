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
  для контента, отдаваемого API.
- Strict-Transport-Security — только при HSTS_ENABLED и запросе по HTTPS.
- Content-Security-Policy — при CSP_ENABLED.

Чистый ASGI (ревью 2026-10-06, BE-28): заголовки дописываются в
http.response.start без буферизации ответа и без отдельной задачи на
каждый запрос, как было у BaseHTTPMiddleware.
"""

from __future__ import annotations

from starlette.datastructures import MutableHeaders

from app.config import settings

_CSP_API = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"

_STATIC_HEADERS = (
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("X-Robots-Tag", "noindex, nofollow"),
    ("Referrer-Policy", "strict-origin-when-cross-origin"),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=()"),
)


class SecurityHeadersMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        https = scope.get("scheme") == "https"

        async def send_with_headers(message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in _STATIC_HEADERS:
                    headers.setdefault(name, value)
                if settings.hsts_enabled and https:
                    headers.setdefault(
                        "Strict-Transport-Security",
                        f"max-age={settings.hsts_max_age_seconds}; includeSubDomains",
                    )
                if settings.csp_enabled:
                    headers.setdefault("Content-Security-Policy", _CSP_API)
            await send(message)

        await self.app(scope, receive, send_with_headers)
