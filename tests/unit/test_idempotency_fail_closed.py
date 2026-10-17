"""
Unit: fail-closed режим idempotency (технический долг, «Idempotency
fail-closed режим»).

Для путей из settings.idempotency_required_paths запрос с Idempotency-Key
при недоступном Redis получает 503, а не выполняется без защиты от
повтора. Остальные пути — fail-open, как раньше.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.middleware.idempotency import IdempotencyMiddleware
from tests.unit.test_redis_module_binding import _FakeRedis


class _BrokenRedis:
    """Redis, который «упал»: любая команда — ConnectionError."""

    async def get(self, *_args, **_kwargs):
        raise ConnectionError("redis down")

    async def set(self, *_args, **_kwargs):
        raise ConnectionError("redis down")

    async def delete(self, *_args, **_kwargs):
        raise ConnectionError("redis down")


@pytest.fixture
def calls() -> list[str]:
    return []


@pytest.fixture
def app(calls, monkeypatch) -> FastAPI:
    monkeypatch.setattr(settings, "idempotency_required_paths", ["/payments/"])
    app = FastAPI()
    app.add_middleware(IdempotencyMiddleware)

    @app.post("/payments/charge")
    async def charge():
        calls.append("charge")
        return {"ok": True}

    @app.post("/notes")
    async def notes():
        calls.append("notes")
        return {"ok": True}

    return app


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


KEY = {"Idempotency-Key": "k-1"}


@pytest.mark.parametrize("redis", [None, _BrokenRedis()], ids=["no-client", "down"])
async def test_required_path_without_redis_is_rejected(app, calls, monkeypatch, redis):
    monkeypatch.setattr("app.redis.redis_client", redis)
    async with _client(app) as c:
        r = await c.post("/payments/charge", json={"sum": 100}, headers=KEY)
    assert r.status_code == 503
    assert r.headers["retry-after"] == "5"
    # Главное: операция НЕ выполнена.
    assert calls == []


@pytest.mark.parametrize("redis", [None, _BrokenRedis()], ids=["no-client", "down"])
async def test_other_paths_stay_fail_open(app, calls, monkeypatch, redis):
    monkeypatch.setattr("app.redis.redis_client", redis)
    async with _client(app) as c:
        r = await c.post("/notes", json={}, headers=KEY)
    assert r.status_code == 200
    assert calls == ["notes"]


async def test_required_path_without_key_is_not_blocked(app, calls, monkeypatch):
    # Без Idempotency-Key защита не запрашивалась — пропускаем как раньше.
    monkeypatch.setattr("app.redis.redis_client", None)
    async with _client(app) as c:
        r = await c.post("/payments/charge", json={})
    assert r.status_code == 200
    assert calls == ["charge"]


async def test_required_path_with_redis_still_deduplicates(app, calls, monkeypatch):
    monkeypatch.setattr("app.redis.redis_client", _FakeRedis())
    async with _client(app) as c:
        first = await c.post("/payments/charge", json={"sum": 1}, headers=KEY)
        second = await c.post("/payments/charge", json={"sum": 1}, headers=KEY)
    assert first.status_code == second.status_code == 200
    assert calls == ["charge"]
