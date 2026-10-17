"""
WebSocket-менеджер для чата поддержки (этап 11).

Назначение:
- Хранить активные WebSocket-соединения **этого** инстанса API (per-ticket).
- Распространять сообщения через Redis Pub/Sub, чтобы при scale api=N
  сообщение, отправленное на инстанс A, доехало до клиента на инстансе B.

Архитектура:

  client A          [API #1]
     ws  ─────────►  conn_map[ticket_id] = {ws_a}
                      │
                      │ publish "support:T1" {body}
                      ▼
                  ┌──────────┐
                  │  Redis   │   pub/sub channel "support:T1"
                  └──────────┘
                      ▲
                      │ subscribe (фоновая задача в каждом инстансе)
                      │
                      ▼
                   [API #2]
                  conn_map[ticket_id] = {ws_b}
                                ▼
                          client B (получает сообщение)

Один инстанс держит ОДНО соединение Redis Pub/Sub на namespace (ревью
2026-10-06, BE-21): каналы ключей подписываются на нём динамически — при
первом локальном сокете ключа — и отписываются при последнем. Сообщения
читает одна фоновая задача менеджера. Раньше на каждый ключ (то есть на
каждого пользователя с открытыми уведомлениями) открывалось своё
соединение с Redis: тысяча пользователей онлайн — тысяча соединений.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from fastapi import WebSocket

# Импортируем МОДУЛЬ, а не значение: init_redis() переприсваивает
# app.redis.redis_client уже после импорта. `from app.redis import
# redis_client` связал бы вечный стейл-None — подписка/публикация в Redis
# никогда бы не включались. Тот же приём (и подробный комментарий) — в
# app/routers/health.py.
from app import redis as redis_state

logger = logging.getLogger(__name__)

# Пауза перед переподпиской после сбоя Redis (удваивается до максимума).
RESUBSCRIBE_DELAY_SECONDS = 1.0
_RESUBSCRIBE_MAX_DELAY_SECONDS = 30.0
# Сколько ждём отправку одному сокету; дольше — клиент считается мёртвым.
SEND_TIMEOUT_SECONDS = 5.0
# Потолок одной команды SUBSCRIBE/UNSUBSCRIBE: при недоступном Redis она
# не должна надолго держать lock менеджера (а с ним connect/disconnect).
REDIS_COMMAND_TIMEOUT_SECONDS = 5.0
# Как часто читатель просыпается без сообщений — проверить, не пора ли
# пересоздать подписку или завершиться.
POLL_SECONDS = 1.0


class WSConnectionManager:
    """
    Per-instance state. На один процесс API — один экземпляр на namespace.

    Менеджер параметризован префиксом Redis-канала (этап 16): тот же
    механизм cross-instance доставки переиспользуется и чатом поддержки
    (`support:{ticket_id}`), и realtime-уведомлениями (`notif:{user_id}`).
    Ключ — произвольный UUID: для поддержки это ticket_id, для
    уведомлений — user_id.

    Структуры:
    - `_connections`: key → set[WebSocket] — активные сокеты ЭТОГО инстанса.
    - `_pubsub`: одно Redis Pub/Sub-соединение, подписанное на каналы всех
      ключей из `_connections`.
    - `_reader`: фоновая задача, читающая `_pubsub` и раздающая сообщения.
      Живёт, пока есть хоть один локальный сокет.
    - `_delivery`: key → последняя задача доставки: сообщения одного ключа
      уходят по порядку, а медленный ключ не задерживает остальные.
    """

    def __init__(self, channel_prefix: str) -> None:
        self._prefix = channel_prefix
        self._connections: dict[uuid.UUID, set[WebSocket]] = {}
        self._pubsub: Any | None = None
        self._reader: asyncio.Task | None = None
        # Подписка разошлась с _connections (SUBSCRIBE не дошёл до Redis) —
        # читатель пересоздаст её целиком.
        self._resync = False
        self._delivery: dict[uuid.UUID, asyncio.Task] = {}
        # aclose() вызван: читатель выходит сам, не полагаясь только на
        # cancel() (отмену может «проглотить» таймаут внутри чтения).
        self._closing = False
        self._lock = asyncio.Lock()

    def _channel(self, key: uuid.UUID) -> str:
        """Имя Redis-канала для ключа: '<prefix>:<key>'."""
        return f"{self._prefix}:{key}"

    def _key(self, channel: Any) -> uuid.UUID | None:
        """Обратное к _channel; None — канал не наш или битый."""
        if isinstance(channel, bytes):
            channel = channel.decode()
        prefix = f"{self._prefix}:"
        if not isinstance(channel, str) or not channel.startswith(prefix):
            return None
        try:
            return uuid.UUID(channel[len(prefix):])
        except ValueError:
            return None

    # -----------------------------------------------------------------
    # Connect / disconnect
    # -----------------------------------------------------------------

    async def connect(self, ticket_id: uuid.UUID, websocket: WebSocket) -> None:
        """
        Регистрирует ws. Первый сокет ключа на этом инстансе подписывает
        общий Pub/Sub на канал ключа (или запускает читателя, который сам
        подпишется на все ключи).

        WebSocket уже должен быть `.accept()`-нут роутером.
        """
        async with self._lock:
            conns = self._connections.setdefault(ticket_id, set())
            first = not conns
            conns.add(websocket)
            if redis_state.redis_client is None or self._closing:
                return
            if self._reader is None or self._reader.done():
                self._reader = asyncio.create_task(self._read_loop())
            elif first and self._pubsub is not None:
                await self._redis_command(
                    self._pubsub.subscribe(self._channel(ticket_id))
                )

    async def disconnect(
        self, ticket_id: uuid.UUID, websocket: WebSocket
    ) -> None:
        """
        Снимает ws с регистрации. Последний сокет ключа отписывает канал;
        когда сокетов не осталось вовсе, читатель закрывает соединение сам.
        """
        async with self._lock:
            conns = self._connections.get(ticket_id)
            if not conns or websocket not in conns:
                return
            conns.discard(websocket)
            if conns:
                return
            self._connections.pop(ticket_id, None)
            if self._pubsub is not None:
                await self._redis_command(
                    self._pubsub.unsubscribe(self._channel(ticket_id))
                )

    async def _redis_command(self, command) -> None:
        """SUBSCRIBE/UNSUBSCRIBE с таймаутом. Сбой → пересоздать подписку."""
        try:
            await asyncio.wait_for(command, REDIS_COMMAND_TIMEOUT_SECONDS)
        except Exception as e:  # noqa: BLE001 — сбой Redis не роняет WS
            logger.warning("Redis pubsub command failed (%s): %s", self._prefix, e)
            self._resync = True

    # -----------------------------------------------------------------
    # Publish / broadcast
    # -----------------------------------------------------------------

    async def publish(self, ticket_id: uuid.UUID, payload: dict) -> None:
        """
        Публикует сообщение в Redis. Все инстансы (включая нас самих)
        получат его через читателя и разошлют в локальные сокеты.

        Если Redis недоступен — fallback: рассылаем только локальным
        клиентам. Это деградация (другие инстансы пропустят), но не
        падение.
        """
        client = redis_state.redis_client
        if client is not None:
            try:
                await client.publish(
                    self._channel(ticket_id), json.dumps(payload)
                )
                return
            except Exception as e:  # noqa: BLE001
                logger.warning("Redis publish failed: %s", e)
        # Fallback: рассылаем локально, поскольку Redis недоступен.
        await self._broadcast_local(ticket_id, payload)

    async def _broadcast_local(
        self, ticket_id: uuid.UUID, payload: dict
    ) -> None:
        """Отправляет сообщение всем сокетам этого инстанса для тикета."""
        conns = list(self._connections.get(ticket_id, set()))
        # Рассылка не под lock'ом: send может занять время, а блокировать
        # connect/disconnect других тикетов из-за этого не хочется.
        # Параллельно и с таймаутом (ревью 2026-10-06, BE-21): раньше один
        # медленный клиент задерживал доставку всем остальным.
        results = await asyncio.gather(
            *(
                asyncio.wait_for(ws.send_json(payload), SEND_TIMEOUT_SECONDS)
                for ws in conns
            ),
            return_exceptions=True,
        )
        dead: list[WebSocket] = []
        for ws, result in zip(conns, results):
            if isinstance(result, BaseException):
                # Рвём сокет на любой ошибке, включая таймаут.
                logger.warning("WS send failed, dropping: %r", result)
                dead.append(ws)
        if dead:
            async with self._lock:
                live = self._connections.get(ticket_id)
                if live is not None:
                    for ws in dead:
                        live.discard(ws)

    def _dispatch(self, key: uuid.UUID, payload: Any) -> None:
        """
        Доставка без ожидания: читатель сразу берёт следующее сообщение, и
        зависший сокет одного ключа не тормозит другие ключи. Внутри ключа
        порядок сохраняется — каждая доставка ждёт предыдущую.
        """
        previous = self._delivery.get(key)

        async def _deliver() -> None:
            if previous is not None:
                await asyncio.gather(previous, return_exceptions=True)
            await self._broadcast_local(key, payload)

        task = asyncio.create_task(_deliver())
        self._delivery[key] = task

        def _cleanup(done: asyncio.Task) -> None:
            if self._delivery.get(key) is done:
                self._delivery.pop(key, None)

        task.add_done_callback(_cleanup)

    # -----------------------------------------------------------------
    # Redis reader loop
    # -----------------------------------------------------------------

    async def _read_loop(self) -> None:
        """
        Фоновая задача: держит общий Pub/Sub, читает сообщения и раздаёт
        их по ключам. При сбое Redis пересоздаёт соединение и подписку с
        backoff, пока есть локальные сокеты (ревью 2026-10-06, BE-21:
        доставка не должна молча прекращаться после сбоя).
        """
        delay = RESUBSCRIBE_DELAY_SECONDS
        try:
            while True:
                async with self._lock:
                    client = redis_state.redis_client
                    if not self._connections or client is None or self._closing:
                        # Закрываемся и снимаем себя под lock'ом: connect,
                        # пришедший сразу после, увидит _reader=None и
                        # запустит нового читателя, а не будет ждать этого.
                        await self._close_pubsub()
                        if self._reader is asyncio.current_task():
                            self._reader = None
                        return
                    try:
                        await self._open(client)
                        opened = True
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "PubSub (%s) subscribe failed, retry in %.1fs",
                            self._prefix, delay,
                        )
                        await self._close_pubsub()
                        opened = False
                if opened:
                    try:
                        await self._read_messages()
                        delay = RESUBSCRIBE_DELAY_SECONDS
                        continue
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "PubSub (%s) reader crashed, resubscribing in %.1fs",
                            self._prefix, delay,
                        )
                await asyncio.sleep(delay)
                delay = min(delay * 2, _RESUBSCRIBE_MAX_DELAY_SECONDS)
        finally:
            await self._close_pubsub()

    async def _open(self, client) -> None:
        """Новое Pub/Sub-соединение на каналы всех ключей. Вызывать под lock'ом."""
        await self._close_pubsub()
        self._resync = False
        pubsub = client.pubsub()
        self._pubsub = pubsub
        channels = [self._channel(key) for key in self._connections]
        await asyncio.wait_for(
            pubsub.subscribe(*channels), REDIS_COMMAND_TIMEOUT_SECONDS
        )

    async def _read_messages(self) -> None:
        """Читает, пока есть сокеты; возвращается, чтобы пересоздать подписку."""
        pubsub = self._pubsub
        if pubsub is None:
            return
        while (
            self._connections
            and not self._resync
            and not self._closing
            and pubsub is self._pubsub
        ):
            message = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=POLL_SECONDS
            )
            if not message or message.get("type") != "message":
                continue
            key = self._key(message.get("channel"))
            if key is None:
                continue
            try:
                data: Any = json.loads(message["data"])
            except (ValueError, TypeError):
                logger.warning(
                    "Bad pubsub payload on %s: %r",
                    message.get("channel"), message.get("data"),
                )
                continue
            self._dispatch(key, data)

    async def _close_pubsub(self) -> None:
        pubsub, self._pubsub = self._pubsub, None
        if pubsub is None:
            return
        try:
            await pubsub.aclose()
        except Exception as e:  # noqa: BLE001
            logger.warning("PubSub close failed: %s", e)

    async def aclose(self) -> None:
        """Остановить читателя и закрыть соединение (shutdown, тесты)."""
        self._closing = True
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        deliveries = list(self._delivery.values())
        for task in deliveries:
            task.cancel()
        await asyncio.gather(*deliveries, return_exceptions=True)
        await self._close_pubsub()


# По одному менеджеру на namespace. Импортируются в соответствующих
# роутерах: ws_manager — чат поддержки, notif_ws_manager — realtime
# уведомления (этап 16). Разные префиксы → разные Redis-каналы, инстансы
# не пересекаются.
ws_manager = WSConnectionManager("support")
notif_ws_manager = WSConnectionManager("notif")
