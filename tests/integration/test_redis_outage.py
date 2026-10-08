"""
Интеграция: поведение при недоступном Redis (ревью 2026-10-06, BE-02).

- init_redis при недоступном Redis всё равно создаёт клиент: redis-py сам
  переподключится, когда Redis вернётся. Раньше клиент оставался None до
  рестарта процесса — idempotency, дедуп рекламы, pub/sub и ВСЕ cron-задачи
  молча отключались.
- Публичные ручки с rate-limit'ом fail-open отдают 200, auth — 503
  (fail_closed), как и обещают комментарии bug_247.
"""

from __future__ import annotations

import pytest_asyncio
from redis.asyncio import Redis

from app import redis as redis_state
from app.config import settings
from app.database import get_db
from app.main import app
from app.redis import get_redis

# Порт 1 на localhost: соединение отклоняется сразу, без таймаутов.
_DEAD_REDIS_URL = "redis://127.0.0.1:1/0"


async def test_init_redis_keeps_client_when_unreachable(monkeypatch):
    monkeypatch.setattr(settings, "redis_url", _DEAD_REDIS_URL)
    monkeypatch.setattr(redis_state, "redis_client", None)
    await redis_state.init_redis()
    try:
        assert redis_state.redis_client is not None
        assert await get_redis() is redis_state.redis_client
    finally:
        await redis_state.close_redis()


@pytest_asyncio.fixture
async def dead_redis_client(db_session):
    from httpx import ASGITransport, AsyncClient

    dead = Redis.from_url(_DEAD_REDIS_URL, decode_responses=True)

    async def _get_db_override():
        yield db_session

    async def _get_redis_override():
        return dead

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_redis] = _get_redis_override
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            yield ac
    finally:
        app.dependency_overrides.clear()
        await dead.aclose()


async def test_public_endpoint_fail_open_without_redis(dead_redis_client):
    r = await dead_redis_client.get("/references/breeds", params={"per_page": 1})
    assert r.status_code == 200, r.text


async def test_auth_endpoint_fail_closed_without_redis(dead_redis_client):
    r = await dead_redis_client.post(
        "/auth/login", json={"email": "x@example.com", "password": "whatever1"}
    )
    assert r.status_code == 503, r.text
