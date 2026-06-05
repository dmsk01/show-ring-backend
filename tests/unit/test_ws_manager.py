"""
Unit-тесты WSConnectionManager (чат поддержки, этап 11).

Фокус — без БД и без живого Redis: подменяем app.redis.redis_client
фейковым клиентом с in-memory pub/sub и проверяем инварианты менеджера:
- первый connect ДЕЙСТВИТЕЛЬНО подписывается на канал Redis;
- одна подписка на ticket даже при нескольких/параллельных connect'ах;
- опубликованное сообщение доезжает до подключённого сокета ровно один раз;
- disconnect последнего сокета отменяет подписку и отписывается от канала.

Регрессия (главная): раньше ws_manager делал `from app.redis import
redis_client` — связывал стейл-None ДО init_redis(), и подписка/публикация
в Redis молча не включались. Эти тесты упали бы на той версии: фейк,
выставленный в app.redis.redis_client, до менеджера бы не дошёл.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections import defaultdict

import pytest
import pytest_asyncio

from app.services.ws_manager import WSConnectionManager


# ---------------------------------------------------------------------
# Фейки: Redis pub/sub и WebSocket
# ---------------------------------------------------------------------


class _FakePubSub:
    """In-memory подписчик: своя очередь, регистрируется в общем hub'е."""

    def __init__(self, hub: dict[str, list[asyncio.Queue]]) -> None:
        self._hub = hub
        self._queue: asyncio.Queue = asyncio.Queue()
        self._channel: str | None = None

    async def subscribe(self, channel: str) -> None:
        self._channel = channel
        self._hub[channel].append(self._queue)
        # Системный subscribe-ack — менеджер обязан его отфильтровать
        # (type != "message"), проверяем заодно и это.
        await self._queue.put({"type": "subscribe", "data": 1})

    def listen(self):
        async def _gen():
            while True:
                yield await self._queue.get()

        return _gen()

    async def unsubscribe(self, channel: str | None = None) -> None:
        ch = channel or self._channel
        subs = self._hub.get(ch or "", [])
        if self._queue in subs:
            subs.remove(self._queue)

    async def aclose(self) -> None:  # noqa: D401 — совместимость с redis-py
        pass


class _FakeRedis:
    """Минимальный async-Redis: pubsub() + publish() поверх общего hub'а."""

    def __init__(self) -> None:
        self._hub: dict[str, list[asyncio.Queue]] = defaultdict(list)

    def pubsub(self) -> _FakePubSub:
        return _FakePubSub(self._hub)

    async def publish(self, channel: str, data: str) -> int:
        subs = list(self._hub.get(channel, []))
        for q in subs:
            q.put_nowait({"type": "message", "data": data})
        return len(subs)

    def subscriber_count(self, channel: str) -> int:
        return len(self._hub.get(channel, []))


class _FakeWS:
    """WebSocket-заглушка: копит отправленные payload'ы."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)


# ---------------------------------------------------------------------
# Хелперы и фикстуры
# ---------------------------------------------------------------------


async def _wait_for(predicate, timeout: float = 1.0) -> None:
    """Опрашивает predicate до True или таймаута (фон. таска успевает стартовать)."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("условие не выполнилось за отведённое время")


@pytest.fixture
def fake_redis(monkeypatch) -> _FakeRedis:
    fake = _FakeRedis()
    # Менеджер читает redis_state.redis_client (через модуль) — патчим
    # именно атрибут модуля app.redis. На стейл-импортной версии этот
    # патч до менеджера бы не дошёл, и тесты подписки упали бы.
    monkeypatch.setattr("app.redis.redis_client", fake)
    return fake


@pytest_asyncio.fixture
async def manager():
    # Префикс "support" → каналы вида support:{ticket} (этап 16
    # параметризовал менеджер; чат поддержки сохранил свой namespace).
    mgr = WSConnectionManager("support")
    yield mgr
    # Чистим фоновые listen-таски, чтобы не текли между тестами.
    tasks = list(mgr._subscriptions.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


# ---------------------------------------------------------------------
# Тесты
# ---------------------------------------------------------------------


async def test_first_connect_subscribes_to_redis(fake_redis, manager):
    """Первый connect создаёт подписку и реально подписывается на канал."""
    ticket = uuid.uuid4()
    await manager.connect(ticket, _FakeWS())

    assert len(manager._subscriptions) == 1
    # listen-таска успевает выполнить subscribe в hub'е — это и ловит
    # стейл-None баг: на нём подписки бы не случилось.
    await _wait_for(lambda: fake_redis.subscriber_count(f"support:{ticket}") == 1)


async def test_concurrent_connects_single_subscription(fake_redis, manager):
    """Параллельные первые connect'ы на один ticket → ровно одна подписка."""
    ticket = uuid.uuid4()
    await asyncio.gather(
        manager.connect(ticket, _FakeWS()),
        manager.connect(ticket, _FakeWS()),
        manager.connect(ticket, _FakeWS()),
    )

    assert len(manager._subscriptions) == 1
    # И в Redis — один подписчик, а не три (иначе сообщения задвоятся).
    await _wait_for(lambda: fake_redis.subscriber_count(f"support:{ticket}") == 1)
    assert fake_redis.subscriber_count(f"support:{ticket}") == 1


async def test_published_message_reaches_socket_once(fake_redis, manager):
    """Опубликованное сообщение доходит до подключённого сокета ровно один раз."""
    ticket = uuid.uuid4()
    ws = _FakeWS()
    await manager.connect(ticket, ws)
    await _wait_for(lambda: fake_redis.subscriber_count(f"support:{ticket}") == 1)

    await manager.publish(ticket, {"body": "привет"})

    await _wait_for(lambda: ws.sent == [{"body": "привет"}])
    # Ровно одно сообщение — ни потери, ни дубля (двойная подписка дала бы 2).
    assert ws.sent == [{"body": "привет"}]


async def test_publish_serializes_payload_through_redis(fake_redis, manager):
    """publish уходит в Redis как JSON-строка (а не fallback на локальную рассылку)."""
    ticket = uuid.uuid4()
    await manager.connect(ticket, _FakeWS())
    await _wait_for(lambda: fake_redis.subscriber_count(f"support:{ticket}") == 1)

    captured: list[str] = []
    original = fake_redis.publish

    async def _spy(channel, data):
        captured.append(data)
        return await original(channel, data)

    fake_redis.publish = _spy  # type: ignore[method-assign]
    await manager.publish(ticket, {"body": "x"})

    assert captured == [json.dumps({"body": "x"})]


async def test_disconnect_last_cancels_subscription(fake_redis, manager):
    """disconnect последнего сокета отменяет подписку и отписывается от канала."""
    ticket = uuid.uuid4()
    ws = _FakeWS()
    await manager.connect(ticket, ws)
    await _wait_for(lambda: fake_redis.subscriber_count(f"support:{ticket}") == 1)
    task = manager._subscriptions[ticket]

    await manager.disconnect(ticket, ws)

    assert ticket not in manager._subscriptions
    assert ticket not in manager._connections
    # Таска отменяется, finally в _listen отписывается от канала Redis.
    await _wait_for(lambda: task.done())
    await _wait_for(lambda: fake_redis.subscriber_count(f"support:{ticket}") == 0)


async def test_second_socket_does_not_add_subscription(fake_redis, manager):
    """Второй сокет на тот же ticket переиспользует подписку, не создаёт новую."""
    ticket = uuid.uuid4()
    ws1, ws2 = _FakeWS(), _FakeWS()
    await manager.connect(ticket, ws1)
    await manager.connect(ticket, ws2)
    await _wait_for(lambda: fake_redis.subscriber_count(f"support:{ticket}") == 1)

    assert len(manager._subscriptions) == 1
    # Сообщение получают оба локальных сокета.
    await manager.publish(ticket, {"body": "всем"})
    await _wait_for(lambda: ws1.sent and ws2.sent)
    assert ws1.sent == [{"body": "всем"}]
    assert ws2.sent == [{"body": "всем"}]


async def test_prefix_isolation(fake_redis):
    """
    Этап 16: два менеджера с разными префиксами на ОДИН и тот же ключ
    не пересекаются по Redis-каналам. publish в support-менеджере не
    долетает до сокета notif-менеджера (разные namespace'ы).
    """
    support = WSConnectionManager("support")
    notif = WSConnectionManager("notif")
    key = uuid.uuid4()  # один и тот же UUID-ключ в обоих менеджерах
    ws_support, ws_notif = _FakeWS(), _FakeWS()
    try:
        await support.connect(key, ws_support)
        await notif.connect(key, ws_notif)
        # Разные каналы: support:{key} и notif:{key}.
        await _wait_for(lambda: fake_redis.subscriber_count(f"support:{key}") == 1)
        await _wait_for(lambda: fake_redis.subscriber_count(f"notif:{key}") == 1)

        await support.publish(key, {"body": "тикет"})
        await _wait_for(lambda: ws_support.sent == [{"body": "тикет"}])
        # notif-сокет НЕ получил сообщение из support-канала.
        assert ws_notif.sent == []

        await notif.publish(key, {"type": "notification"})
        await _wait_for(lambda: ws_notif.sent == [{"type": "notification"}])
        # support-сокет не получил лишнего — у него по-прежнему одно.
        assert ws_support.sent == [{"body": "тикет"}]
    finally:
        for mgr in (support, notif):
            tasks = list(mgr._subscriptions.values())
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
