import json
from typing import Any
import bleach
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


def _sanitize(value: Any) -> Any:
    if isinstance(value, str):
        return bleach.clean(value, tags=[], strip=True)
    if isinstance(value, dict):
        return {k: _sanitize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize(v) for v in value]
    return value


class SanitizationMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        content_type = request.headers.get("content-type", "")
        if not content_type.startswith("application/json"):
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

        sanitized = _sanitize(data)
        new_body = json.dumps(sanitized).encode("utf-8")

        # переопределяем receive, чтобы downstream получил очищенное тело
        async def receive():
            return {"type": "http.request", "body": new_body, "more_body": False}

        request._receive = receive
        return await call_next(request)