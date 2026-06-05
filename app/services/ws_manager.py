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

Один инстанс держит ОДНУ подписку на канал per ticket_id (defer-init:
подписка создаётся при первом ws connect, закрывается при последнем
disconnect). Это экономит соединения с Redis при тысячах активных
тикетов.

Замечание про graceful shutdown: при остановке приложения текущие
asyncio.Task'и pubsub-листенеров отменяются автоматически при закрытии
loop'а. Корректно завершать их через cancel() — задача на этап 14.
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


class WSConnectionManager:
    """
    Per-instance state. На один процесс API — один экземпляр на namespace.

    Менеджер параметризован префиксом Redis-канала (этап 16): тот же
    выверенный механизм cross-instance доставки переиспользуется и чатом
    поддержки (`support:{ticket_id}`), и realtime-уведомлениями
    (`notif:{user_id}`). Ключ — произвольный UUID: для поддержки это
    ticket_id, для уведомлений — user_id.

    Структуры:
    - `_connections`: key → set[WebSocket] — активные сокеты ЭТОГО
      инстанса.
    - `_subscriptions`: key → asyncio.Task — фоновая корутина,
      слушающая Redis pubsub для этого ключа.
    """

    def __init__(self, channel_prefix: str) -> None:
        self._prefix = channel_prefix
        self._connections: dict[uuid.UUID, set[WebSocket]] = {}
        self._subscriptions: dict[uuid.UUID, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    def _channel(self, key: uuid.UUID) -> str:
        """Имя Redis-канала для ключа: '<prefix>:<key>'."""
        return f"{self._prefix}:{key}"

    # -----------------------------------------------------------------
    # Connect / disconnect
    # -----------------------------------------------------------------

    async def connect(self, ticket_id: uuid.UUID, websocket: WebSocket) -> None:
        """
        Регистрирует ws и при первом соединении на этом инстансе для
        этого ticket'а — подписывает инстанс на Redis-канал.

        WebSocket уже должен быть `.accept()`-нут роутером.
        """
        # Проверка и регистрация подписки — под ОДНИМ lock'ом (раньше запись
        # self._subscriptions делалась вне его). В однопоточном asyncio между
        # чтением и записью нет await, поэтому реальной гонки не было, но
        # держать оба шага под lock'ом надёжнее на будущее: появись внутри
        # блока await — окно двойной подписки не откроется. asyncio.create_task
        # синхронный (только планирует корутину), так что неблокирующий
        # asyncio.Lock через него держать безопасно.
        async with self._lock:
            conns = self._connections.setdefault(ticket_id, set())
            conns.add(websocket)
            if (
                ticket_id not in self._subscriptions
                and redis_state.redis_client is not None
            ):
                self._subscriptions[ticket_id] = asyncio.create_task(
                    self._listen(ticket_id)
                )

    async def disconnect(
        self, ticket_id: uuid.UUID, websocket: WebSocket
    ) -> None:
        """
        Снимает ws с регистрации. Если это был последний — отменяет
        Redis-подписку для тикета (экономия соединений).
        """
        cancel_task: asyncio.Task | None = None
        async with self._lock:
            conns = self._connections.get(ticket_id)
            if conns and websocket in conns:
                conns.discard(websocket)
                if not conns:
                    self._connections.pop(ticket_id, None)
                    cancel_task = self._subscriptions.pop(ticket_id, None)

        if cancel_task is not None:
            cancel_task.cancel()
            # await не нужен — Task сам корректно завершится в фоне.

    # -----------------------------------------------------------------
    # Publish / broadcast
    # -----------------------------------------------------------------

    async def publish(self, ticket_id: uuid.UUID, payload: dict) -> None:
        """
        Публикует сообщение в Redis. Все инстансы (включая нас самих)
        получат его через подписчика _listen и разошлют в локальные
        сокеты.

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
        dead: list[WebSocket] = []
        for ws in conns:
            try:
                await ws.send_json(payload)
            except Exception as e:  # noqa: BLE001 — рвём сокет на любой ошибке
                logger.warning("WS send failed, dropping: %s", e)
                dead.append(ws)
        if dead:
            async with self._lock:
                live = self._connections.get(ticket_id)
                if live is not None:
                    for ws in dead:
                        live.discard(ws)

    # -----------------------------------------------------------------
    # Redis subscriber loop
    # -----------------------------------------------------------------

    async def _listen(self, ticket_id: uuid.UUID) -> None:
        """
        Фоновая задача: подписывается на support:{ticket_id} в Redis,
        и при получении сообщения раскидывает в локальные WS.

        При cancel() корректно завершается через try/finally.
        """
        client = redis_state.redis_client
        if client is None:
            return
        pubsub = client.pubsub()
        channel = self._channel(ticket_id)
        try:
            await pubsub.subscribe(channel)
            async for message in pubsub.listen():
                # listen() отдаёт системные сообщения (subscribe-ack
                # и т.п.) с type != "message" — фильтруем.
                if message.get("type") != "message":
                    continue
                try:
                    data: Any = json.loads(message["data"])
                except (ValueError, TypeError):
                    logger.warning(
                        "Bad pubsub payload on %s: %r",
                        channel, message.get("data"),
                    )
                    continue
                await self._broadcast_local(ticket_id, data)
        except asyncio.CancelledError:
            # Нормальное завершение при последнем disconnect.
            raise
        except Exception:
            logger.exception("PubSub listener crashed for %s", channel)
        finally:
            try:
                await pubsub.unsubscribe(channel)
                await pubsub.aclose()
            except Exception as e:  # noqa: BLE001
                logger.warning("PubSub close failed: %s", e)


# По одному менеджеру на namespace. Импортируются в соответствующих
# роутерах: ws_manager — чат поддержки, notif_ws_manager — realtime
# уведомления (этап 16). Разные префиксы → разные Redis-каналы, инстансы
# не пересекаются.
ws_manager = WSConnectionManager("support")
notif_ws_manager = WSConnectionManager("notif")
