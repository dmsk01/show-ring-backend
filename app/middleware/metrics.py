"""
Счётчики ответов 5xx и медленных запросов (план защиты 2026-10-05, этап 3).

Чистый ASGI-middleware (не BaseHTTPMiddleware): не буферизует ответ и не
мешает WebSocket — их пропускаем как есть.
"""

from __future__ import annotations

import time

from app.config import settings
from app.services import security_metrics


class MetricsMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.monotonic()
        status_code = 500

        async def send_wrapper(message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            if status_code >= 500:
                await security_metrics.record(security_metrics.HTTP_5XX)
            if time.monotonic() - started > settings.slow_request_seconds:
                await security_metrics.record(security_metrics.SLOW_REQUEST)
