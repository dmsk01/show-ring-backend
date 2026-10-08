"""
Unit: «лёгкие» middleware — чистый ASGI, а не BaseHTTPMiddleware
(ревью 2026-10-06, BE-28).

BaseHTTPMiddleware заворачивает каждый запрос в отдельную задачу и поток
памяти, ломает contextvars/BackgroundTasks и не видит WebSocket. Для
middleware, которые лишь читают заголовки запроса и дописывают заголовки
ответа, это чистые накладные расходы на каждый запрос.
"""

from __future__ import annotations

import pytest
from starlette.middleware.base import BaseHTTPMiddleware

from app.middleware.csrf import CSRFMiddleware
from app.middleware.proxy_headers import ProxyHeadersMiddleware
from app.middleware.request_id import RequestIdMiddleware
from app.middleware.security_headers import SecurityHeadersMiddleware


@pytest.mark.parametrize(
    "cls",
    [RequestIdMiddleware, CSRFMiddleware, SecurityHeadersMiddleware, ProxyHeadersMiddleware],
)
def test_middleware_is_pure_asgi(cls):
    assert not issubclass(cls, BaseHTTPMiddleware)


async def test_security_headers_and_request_id_on_response(client):
    r = await client.get("/health/")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers.get("x-request-id")


async def test_incoming_request_id_is_echoed(client):
    rid = "11111111-2222-3333-4444-555555555555"
    r = await client.get("/health/", headers={"X-Request-ID": rid})
    assert r.headers["x-request-id"] == rid
