"""
WSConnectionManager на настоящем Redis (ревью 2026-10-06, BE-21).

Unit-тесты гоняют менеджер на фейке; здесь проверяется то, что фейк не
доказывает: redis-py позволяет подписывать новые каналы на Pub/Sub-
соединении, пока фоновая задача читает его, и все ключи живут на одном
соединении.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest_asyncio

from app.services.ws_manager import WSConnectionManager


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)


async def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("условие не выполнилось за отведённое время")


@pytest_asyncio.fixture
async def manager(test_redis, monkeypatch):
    monkeypatch.setattr("app.redis.redis_client", test_redis)
    mgr = WSConnectionManager(f"test-{uuid.uuid4().hex[:8]}")
    yield mgr
    await mgr.aclose()


async def _subscribers(redis, channel: str) -> int:
    result = await redis.pubsub_numsub(channel)
    return int(result[0][1])


async def test_keys_share_one_connection_and_receive_messages(test_redis, manager):
    a, b = uuid.uuid4(), uuid.uuid4()
    ws_a, ws_b = _FakeWS(), _FakeWS()
    await manager.connect(a, ws_a)

    async def a_subscribed() -> bool:
        return await _subscribers(test_redis, manager._channel(a)) == 1

    await _wait_for(a_subscribed)

    # Второй ключ подписывается, пока читатель уже ждёт сообщений.
    await manager.connect(b, ws_b)

    async def b_subscribed() -> bool:
        return await _subscribers(test_redis, manager._channel(b)) == 1

    await _wait_for(b_subscribed)
    # Оба канала — на одном соединении.
    channels = await test_redis.pubsub_channels(f"{manager._prefix}:*")
    assert len(channels) == 2
    clients = await test_redis.client_list()
    pubsub_clients = [c for c in clients if int(c.get("sub", 0)) > 0]
    assert any(int(c["sub"]) == 2 for c in pubsub_clients)

    await manager.publish(a, {"body": "A"})
    await manager.publish(b, {"body": "B"})

    async def delivered() -> bool:
        return ws_a.sent == [{"body": "A"}] and ws_b.sent == [{"body": "B"}]

    await _wait_for(delivered)


async def test_disconnect_unsubscribes_channel(test_redis, manager):
    key = uuid.uuid4()
    ws = _FakeWS()
    await manager.connect(key, ws)

    async def subscribed() -> bool:
        return await _subscribers(test_redis, manager._channel(key)) == 1

    await _wait_for(subscribed)

    await manager.disconnect(key, ws)

    async def unsubscribed() -> bool:
        return await _subscribers(test_redis, manager._channel(key)) == 0

    await _wait_for(unsubscribed)
