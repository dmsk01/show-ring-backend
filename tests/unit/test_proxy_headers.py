"""Unit: ProxyHeadersMiddleware не даёт клиенту подменить свой IP.

nginx с $proxy_add_x_forwarded_for ДОПИСЫВАЕТ реальный адрес к заголовку,
присланному клиентом: "X-Forwarded-For: <что угодно>, <реальный IP>".
Доверять можно только правой части — её добавили наши прокси.
"""
from __future__ import annotations

import ipaddress

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from app.middleware import proxy_headers

_PROXY_PEER = ("172.28.0.10", 40000)


async def _whoami(request: Request) -> PlainTextResponse:
    return PlainTextResponse(request.client.host if request.client else "")


def _client(peer: tuple[str, int]) -> httpx.AsyncClient:
    app = Starlette(routes=[Route("/", _whoami)])
    app.add_middleware(proxy_headers.ProxyHeadersMiddleware)
    transport = httpx.ASGITransport(app=app, client=peer)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture(autouse=True)
def _trust_docker_subnet(monkeypatch):
    monkeypatch.setattr(
        proxy_headers, "_TRUSTED_NETS", [ipaddress.ip_network("172.28.0.0/16")]
    )


async def test_spoofed_leftmost_xff_is_ignored():
    # Клиент прислал "1.2.3.4", nginx дописал реальный 203.0.113.7.
    async with _client(_PROXY_PEER) as c:
        r = await c.get("/", headers={"X-Forwarded-For": "1.2.3.4, 203.0.113.7"})
    assert r.text == "203.0.113.7"


async def test_single_xff_from_trusted_proxy_is_used():
    async with _client(_PROXY_PEER) as c:
        r = await c.get("/", headers={"X-Forwarded-For": "203.0.113.7"})
    assert r.text == "203.0.113.7"


async def test_trusted_hops_are_skipped_from_the_right():
    # Цепочка из двух наших прокси: правее клиента стоит доверенный адрес.
    async with _client(_PROXY_PEER) as c:
        r = await c.get(
            "/", headers={"X-Forwarded-For": "1.2.3.4, 203.0.113.7, 172.28.0.3"}
        )
    assert r.text == "203.0.113.7"


async def test_xff_from_untrusted_peer_is_ignored():
    async with _client(("198.51.100.1", 5555)) as c:
        r = await c.get("/", headers={"X-Forwarded-For": "1.2.3.4"})
    assert r.text == "198.51.100.1"
