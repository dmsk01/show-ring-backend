"""
Unit-тесты WSConnectionManager (чат поддержки — этап 11, уведомления — этап 16).

Фокус — без БД и без живого Redis: подменяем app.redis.redis_client
фейковым клиентом с in-memory pub/sub и проверяем инварианты менеджера:
- первый connect ДЕЙСТВИТЕЛЬНО подписывается на канал Redis;
- одно Pub/Sub-соединение на менеджер, сколько бы ключей ни было (BE-21);
- опубликованное сообщение доезжает до подключённого сокета ровно один раз;
- disconnect последнего сокета ключа отписывает канал, последнего вообще —
  закрывает соединение.

Регрессия (главная): раньше ws_manager делал `from app.redis import
redis_client` — связывал стейл-None ДО init_redis(), и подписка/публикация
в Redis молча не включались. Эти тесты упали бы на той версии: фейк,
выставленный в app.redis.redis_client, до менеджера бы не дошёл.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
import pytest_asyncio

from app.services.ws_manager import WSConnectionManager


# ---------------------------------------------------------------------
# Фейки: Redis pub/sub и WebSocket
# ---------------------------------------------------------------------


class _FakePubSub:
    """In-memory Pub/Sub-соединение: одна очередь, много каналов."""

    def __init__(self, redis: "_FakeRedis") -> None:
        self._redis = redis
        self.queue: asyncio.Queue = asyncio.Queue()
        self.channels: set[str] = set()
        self.closed = False

    async def subscribe(self, *channels: str) -> None:
        for ch in channels:
            self.channels.add(ch)
            # Системный subscribe-ack — менеджер обязан его отфильтровать.
            await self.queue.put({"type": "subscribe", "channel": ch, "data": 1})

    async def unsubscribe(self, *channels: str) -> None:
        for ch in channels:
            self.channels.discard(ch)

    async def get_message(self, ignore_subscribe_messages=False, timeout=0.0):
        # asyncio.timeout, а не wait_for: в 3.11 wait_for может проглотить
        # отмену, если очередь отдала элемент в тот же момент.
        try:
            async with asyncio.timeout(timeout):
                msg = await self.queue.get()
        except TimeoutError:
            return None
        if ignore_subscribe_messages and msg["type"] != "message":
            return None
        return msg

    async def aclose(self) -> None:
        self.closed = True
        self.channels.clear()


class _FakeRedis:
    """Минимальный async-Redis: pubsub() + publish()."""

    def __init__(self) -> None:
        self.pubsubs: list[_FakePubSub] = []

    def pubsub(self) -> _FakePubSub:
        ps = _FakePubSub(self)
        self.pubsubs.append(ps)
        return ps

    async def publish(self, channel: str, data: str) -> int:
        receivers = [ps for ps in self.pubsubs if channel in ps.channels]
        for ps in receivers:
            ps.queue.put_nowait({"type": "message", "channel": channel, "data": data})
        return len(receivers)

    def subscriber_count(self, channel: str) -> int:
        return sum(1 for ps in self.pubsubs if channel in ps.channels)

    def open_connections(self) -> int:
        return sum(1 for ps in self.pubsubs if not ps.closed)


class _FakeWS:
    """WebSocket-заглушка: копит отправленные payload'ы."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)


# ---------------------------------------------------------------------
# Хелперы и фикстуры
# ---------------------------------------------------------------------


async def _wait_for(predicate, timeout: float = 2.0) -> None:
    """Опрашивает predicate до True или таймаута (фон. таска успевает стартовать)."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("условие не выполнилось за отведённое время")


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch):
    # Читатель просыпается чаще — тесты на завершение не ждут по секунде.
    monkeypatch.setattr("app.services.ws_manager.POLL_SECONDS", 0.02)


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
    # Префикс "support" → каналы вида support:{ticket}.
    mgr = WSConnectionManager("support")
    yield mgr
    await mgr.aclose()


# ---------------------------------------------------------------------
# Тесты
# ---------------------------------------------------------------------


async def test_first_connect_subscribes_to_redis(fake_redis, manager):
    """Первый connect реально подписывается на канал ключа."""
    ticket = uuid.uuid4()
    await manager.connect(ticket, _FakeWS())

    await _wait_for(lambda: fake_redis.subscriber_count(f"support:{ticket}") == 1)


async def test_many_keys_share_one_connection(fake_redis, manager):
    """BE-21: сотня ключей — одно Pub/Sub-соединение, а не сотня."""
    tickets = [uuid.uuid4() for _ in range(100)]
    for ticket in tickets:
        await manager.connect(ticket, _FakeWS())

    await _wait_for(
        lambda: all(fake_redis.subscriber_count(f"support:{t}") == 1 for t in tickets)
    )
    assert fake_redis.open_connections() == 1


async def test_concurrent_connects_single_subscription(fake_redis, manager):
    """Параллельные первые connect'ы на один ticket → ровно одна подписка."""
    ticket = uuid.uuid4()
    await asyncio.gather(
        manager.connect(ticket, _FakeWS()),
        manager.connect(ticket, _FakeWS()),
        manager.connect(ticket, _FakeWS()),
    )

    await _wait_for(lambda: fake_redis.subscriber_count(f"support:{ticket}") == 1)
    assert fake_redis.open_connections() == 1


async def test_published_message_reaches_socket_once(fake_redis, manager):
    """Опубликованное сообщение доходит до подключённого сокета ровно один раз."""
    ticket = uuid.uuid4()
    ws = _FakeWS()
    await manager.connect(ticket, ws)
    await _wait_for(lambda: fake_redis.subscriber_count(f"support:{ticket}") == 1)

    await manager.publish(ticket, {"body": "привет"})

    await _wait_for(lambda: ws.sent == [{"body": "привет"}])
    await asyncio.sleep(0.05)
    assert ws.sent == [{"body": "привет"}]


async def test_messages_go_only_to_their_key(fake_redis, manager):
    """Общее соединение не путает ключи: сообщение уходит только своему."""
    a, b = uuid.uuid4(), uuid.uuid4()
    ws_a, ws_b = _FakeWS(), _FakeWS()
    await manager.connect(a, ws_a)
    await manager.connect(b, ws_b)
    await _wait_for(lambda: fake_redis.subscriber_count(f"support:{b}") == 1)

    await manager.publish(a, {"body": "для A"})

    await _wait_for(lambda: ws_a.sent == [{"body": "для A"}])
    assert ws_b.sent == []


async def test_messages_of_one_key_keep_order(fake_redis, manager):
    """Доставка асинхронная, но внутри ключа порядок сохраняется."""
    ticket = uuid.uuid4()
    ws = _FakeWS()
    await manager.connect(ticket, ws)
    await _wait_for(lambda: fake_redis.subscriber_count(f"support:{ticket}") == 1)

    for i in range(20):
        await manager.publish(ticket, {"n": i})

    await _wait_for(lambda: len(ws.sent) == 20)
    assert ws.sent == [{"n": i} for i in range(20)]


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


async def test_disconnect_last_socket_of_key_unsubscribes(fake_redis, manager):
    """Последний сокет ключа отписывает его канал; соединение живёт ради других."""
    a, b = uuid.uuid4(), uuid.uuid4()
    ws_a = _FakeWS()
    await manager.connect(a, ws_a)
    await manager.connect(b, _FakeWS())
    await _wait_for(lambda: fake_redis.subscriber_count(f"support:{b}") == 1)

    await manager.disconnect(a, ws_a)

    assert a not in manager._connections
    assert fake_redis.subscriber_count(f"support:{a}") == 0
    assert fake_redis.subscriber_count(f"support:{b}") == 1
    assert fake_redis.open_connections() == 1


async def test_last_disconnect_closes_connection(fake_redis, manager):
    """Ушёл последний сокет вообще — читатель завершается и закрывает соединение."""
    ticket = uuid.uuid4()
    ws = _FakeWS()
    await manager.connect(ticket, ws)
    await _wait_for(lambda: fake_redis.subscriber_count(f"support:{ticket}") == 1)

    await manager.disconnect(ticket, ws)

    await _wait_for(lambda: manager._reader is None)
    assert fake_redis.open_connections() == 0


async def test_reconnect_after_reader_stopped(fake_redis, manager):
    """После остановки читателя новый connect поднимает его заново."""
    ticket = uuid.uuid4()
    ws = _FakeWS()
    await manager.connect(ticket, ws)
    await _wait_for(lambda: fake_redis.subscriber_count(f"support:{ticket}") == 1)
    await manager.disconnect(ticket, ws)
    await _wait_for(lambda: manager._reader is None)

    ws2 = _FakeWS()
    await manager.connect(ticket, ws2)
    await _wait_for(lambda: fake_redis.subscriber_count(f"support:{ticket}") == 1)
    await manager.publish(ticket, {"body": "снова"})
    await _wait_for(lambda: ws2.sent == [{"body": "снова"}])


async def test_second_socket_does_not_add_subscription(fake_redis, manager):
    """Второй сокет на тот же ticket переиспользует подписку."""
    ticket = uuid.uuid4()
    ws1, ws2 = _FakeWS(), _FakeWS()
    await manager.connect(ticket, ws1)
    await manager.connect(ticket, ws2)
    await _wait_for(lambda: fake_redis.subscriber_count(f"support:{ticket}") == 1)

    await manager.publish(ticket, {"body": "всем"})
    await _wait_for(lambda: ws1.sent and ws2.sent)
    assert ws1.sent == [{"body": "всем"}]
    assert ws2.sent == [{"body": "всем"}]


async def test_prefix_isolation(fake_redis):
    """
    Этап 16: два менеджера с разными префиксами на ОДИН и тот же ключ
    не пересекаются по Redis-каналам.
    """
    support = WSConnectionManager("support")
    notif = WSConnectionManager("notif")
    key = uuid.uuid4()
    ws_support, ws_notif = _FakeWS(), _FakeWS()
    try:
        await support.connect(key, ws_support)
        await notif.connect(key, ws_notif)
        await _wait_for(lambda: fake_redis.subscriber_count(f"support:{key}") == 1)
        await _wait_for(lambda: fake_redis.subscriber_count(f"notif:{key}") == 1)

        await support.publish(key, {"body": "тикет"})
        await _wait_for(lambda: ws_support.sent == [{"body": "тикет"}])
        assert ws_notif.sent == []

        await notif.publish(key, {"type": "notification"})
        await _wait_for(lambda: ws_notif.sent == [{"type": "notification"}])
        assert ws_support.sent == [{"body": "тикет"}]
    finally:
        await support.aclose()
        await notif.aclose()


# ---------------------------------------------------------------------
# Ревью 2026-10-06, BE-21
# ---------------------------------------------------------------------


async def test_reader_resubscribes_after_crash(fake_redis, manager, monkeypatch):
    """Кратковременный сбой Redis не должен навсегда отключать доставку:
    читатель пересоздаёт соединение и подписку на все ключи."""
    monkeypatch.setattr("app.services.ws_manager.RESUBSCRIBE_DELAY_SECONDS", 0.01)
    ticket = uuid.uuid4()
    ws = _FakeWS()
    crashed = {"done": False}
    original_pubsub = fake_redis.pubsub

    def flaky_pubsub():
        ps = original_pubsub()
        if not crashed["done"]:
            crashed["done"] = True

            async def broken_get_message(**_kwargs):
                raise ConnectionError("redis blip")

            ps.get_message = broken_get_message  # type: ignore[method-assign]
        return ps

    fake_redis.pubsub = flaky_pubsub  # type: ignore[method-assign]
    await manager.connect(ticket, ws)
    # Первое соединение сломано и закрыто, второе подписано.
    await _wait_for(lambda: len(fake_redis.pubsubs) == 2)
    await _wait_for(lambda: fake_redis.subscriber_count(f"support:{ticket}") == 1)

    await manager.publish(ticket, {"body": "после сбоя"})
    await _wait_for(lambda: ws.sent == [{"body": "после сбоя"}])
    assert fake_redis.open_connections() == 1


async def test_failed_subscribe_triggers_resync(fake_redis, manager, monkeypatch):
    """SUBSCRIBE нового ключа не дошёл до Redis — читатель пересоздаёт
    подписку, и ключ всё равно начинает получать сообщения."""
    first, second = uuid.uuid4(), uuid.uuid4()
    await manager.connect(first, _FakeWS())
    await _wait_for(lambda: fake_redis.subscriber_count(f"support:{first}") == 1)

    ps = fake_redis.pubsubs[0]

    async def broken_subscribe(*_channels):
        raise ConnectionError("redis blip")

    ps.subscribe = broken_subscribe  # type: ignore[method-assign]
    ws = _FakeWS()
    await manager.connect(second, ws)

    await _wait_for(lambda: fake_redis.subscriber_count(f"support:{second}") == 1)
    await manager.publish(second, {"body": "дошло"})
    await _wait_for(lambda: ws.sent == [{"body": "дошло"}])


async def test_slow_socket_does_not_block_others(fake_redis, manager, monkeypatch):
    """Рассылка параллельная и с таймаутом: зависший клиент не задерживает
    остальных и отключается."""
    monkeypatch.setattr("app.services.ws_manager.SEND_TIMEOUT_SECONDS", 0.05)

    class _StuckWS(_FakeWS):
        async def send_json(self, payload):
            await asyncio.sleep(10)

    ticket = uuid.uuid4()
    stuck, fast = _StuckWS(), _FakeWS()
    await manager.connect(ticket, stuck)
    await manager.connect(ticket, fast)
    started = asyncio.get_event_loop().time()
    await manager._broadcast_local(ticket, {"body": "x"})
    assert asyncio.get_event_loop().time() - started < 1
    assert fast.sent == [{"body": "x"}]
    assert stuck not in manager._connections.get(ticket, set())


async def test_slow_key_does_not_delay_other_keys(fake_redis, manager, monkeypatch):
    """Зависший сокет одного ключа не задерживает доставку другому ключу."""
    monkeypatch.setattr("app.services.ws_manager.SEND_TIMEOUT_SECONDS", 5)

    class _StuckWS(_FakeWS):
        async def send_json(self, payload):
            await asyncio.sleep(10)

    slow_key, fast_key = uuid.uuid4(), uuid.uuid4()
    fast = _FakeWS()
    await manager.connect(slow_key, _StuckWS())
    await manager.connect(fast_key, fast)
    await _wait_for(lambda: fake_redis.subscriber_count(f"support:{fast_key}") == 1)

    await manager.publish(slow_key, {"body": "застрянет"})
    await manager.publish(fast_key, {"body": "быстро"})

    await _wait_for(lambda: fast.sent == [{"body": "быстро"}], timeout=1.0)
