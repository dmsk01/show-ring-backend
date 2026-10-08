"""
Unit: idempotency-кэш изолирован по пользователю и при cookie-аутентификации
(ревью 2026-10-06, BE-09).

Веб-клиент авторизуется httpOnly-кукой access_token, без заголовка
Authorization. Раньше identity строилась только из заголовка, и все
веб-пользователи за одним NAT делили namespace по IP: повтор ключа+тела
другим пользователем получал ЧУЖОЙ кэшированный ответ.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request, Response
from httpx import ASGITransport, AsyncClient

from app.middleware.idempotency import IdempotencyMiddleware
from tests.unit.test_redis_module_binding import _FakeRedis


@pytest.fixture
def fake_redis(monkeypatch) -> _FakeRedis:
    fake = _FakeRedis()
    monkeypatch.setattr("app.redis.redis_client", fake)
    return fake


def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(IdempotencyMiddleware)

    @app.post("/whoami")
    async def whoami(request: Request, response: Response):
        response.set_cookie("session_marker", "secret-value")
        return {"cookie": request.cookies.get("access_token")}

    @app.post("/auth/login")
    async def login(response: Response):
        response.set_cookie("access_token", "fresh-token")
        return {"ok": True}

    return app


def _client(app: FastAPI) -> AsyncClient:
    # Один и тот же IP у обоих «пользователей» — как за NAT.
    transport = ASGITransport(app=app, client=("203.0.113.5", 1000))
    return AsyncClient(transport=transport, base_url="http://t")


async def test_cookie_users_behind_same_ip_do_not_share_cache(fake_redis):
    app = _make_app()
    headers = {"Idempotency-Key": "same-key"}
    async with _client(app) as c:
        r_a = await c.post(
            "/whoami", headers={**headers, "Cookie": "access_token=token-A"}
        )
        r_b = await c.post(
            "/whoami", headers={**headers, "Cookie": "access_token=token-B"}
        )
    assert r_a.json() == {"cookie": "token-A"}
    assert r_b.json() == {"cookie": "token-B"}


async def test_cached_replay_does_not_resend_set_cookie(fake_redis):
    app = _make_app()
    headers = {"Idempotency-Key": "k", "Cookie": "access_token=token-A"}
    async with _client(app) as c:
        await c.post("/whoami", headers=headers)
        replay = await c.post("/whoami", headers=headers)
    assert replay.status_code == 200
    assert not replay.headers.get_list("set-cookie")


async def test_auth_routes_are_not_cached(fake_redis):
    app = _make_app()
    async with _client(app) as c:
        await c.post("/auth/login", headers={"Idempotency-Key": "k"})
    assert not any(k.startswith("idem:") for k in fake_redis.store)


async def test_first_response_keeps_all_set_cookie_headers(fake_redis):
    # dict(response.headers) схлопывал повторяющиеся Set-Cookie в один.
    app = FastAPI()
    app.add_middleware(IdempotencyMiddleware)

    @app.post("/two-cookies")
    async def two_cookies(response: Response):
        response.set_cookie("a", "1")
        response.set_cookie("b", "2")
        return {"ok": True}

    async with _client(app) as c:
        r = await c.post("/two-cookies", headers={"Idempotency-Key": "k2"})
    names = {h.split("=", 1)[0] for h in r.headers.get_list("set-cookie")}
    assert names == {"a", "b"}
