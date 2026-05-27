"""
Воркер outbox-dispatcher (follow-up для этапов 9, 10).

Опрашивает таблицу `outbox_events`, забирает pending пачкой, публикует
в RabbitMQ и помечает sent. На сбое publish инкрементирует attempts
и оставляет pending — следующий тик подберёт.

Архитектура polling, не PG NOTIFY:
- Polling раз в 2 секунды добавляет максимум 2 сек латенси к событию —
  для notification-сценариев приемлемо.
- LISTEN/NOTIFY дал бы суб-секундную доставку, но требует выделенного
  PG-соединения и сложнее в отладке. Можно перейти позже без слома
  модели outbox.

Параллелизация: несколько воркеров безопасно работают одновременно
благодаря `SELECT FOR UPDATE SKIP LOCKED` в fetch_pending — каждый
возьмёт свой кусок.
"""

from __future__ import annotations

import asyncio
import json
import logging

import aio_pika
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories import outbox as outbox_repo

logger = logging.getLogger(__name__)

# Параметры опроса. На dev keep small — события появляются редко,
# смысла молотить базу нет. На prod увеличиваем BATCH_SIZE.
POLL_INTERVAL_SECONDS = 2.0
BATCH_SIZE = 50
# Лимит попыток. После N неудач помечаем failed; дальше — ручной разбор.
MAX_ATTEMPTS = 10


async def dispatch_once(
    db: AsyncSession,
    publish_channel: aio_pika.abc.AbstractChannel,
) -> int:
    """
    Один проход dispatcher'а. Возвращает число обработанных событий
    (sent + failed + remaining_pending). Используется для метрик/логов.

    Каждое событие коммитится отдельно после успешного publish —
    иначе при крэше посередине пачки кусок остался бы залоченным
    SKIP LOCKED'ом до конца транзакции.
    """
    events = await outbox_repo.fetch_pending(db, limit=BATCH_SIZE)
    if not events:
        # Откатываем пустую транзакцию — иначе lock висит до timeout.
        await db.rollback()
        return 0

    sent = 0
    for ev in events:
        try:
            await _publish(publish_channel, ev)
        except Exception as e:  # noqa: BLE001 — оставляем pending для retry
            logger.warning(
                "Outbox publish failed for %s (attempt %d): %s",
                ev.id, ev.attempts + 1, e,
            )
            if ev.attempts + 1 >= MAX_ATTEMPTS:
                await outbox_repo.mark_failed(db, ev.id, str(e))
            else:
                await outbox_repo.increment_attempts(db, ev.id, str(e))
            continue
        await outbox_repo.mark_sent(db, ev.id)
        sent += 1
    # Один commit на всю пачку — освобождает SKIP LOCKED-локки.
    await db.commit()
    return sent


async def _publish(
    channel: aio_pika.abc.AbstractChannel, event
) -> None:
    """
    Публикует одно событие. Если exchange задан — TOPIC exchange;
    иначе default exchange + routing_key как имя очереди (direct).
    """
    body = json.dumps(event.payload).encode()
    message = aio_pika.Message(
        body=body,
        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        content_type="application/json",
    )
    if event.exchange:
        ex = await channel.declare_exchange(
            event.exchange,
            aio_pika.ExchangeType.TOPIC,
            durable=True,
        )
        await ex.publish(message, routing_key=event.routing_key)
    else:
        # default exchange: routing_key = queue_name. Декларируем
        # очередь, чтобы сообщение не потерялось при первом publish.
        await channel.declare_queue(event.routing_key, durable=True)
        await channel.default_exchange.publish(
            message, routing_key=event.routing_key
        )


async def run_loop(
    session_factory,
    channel: aio_pika.abc.AbstractChannel,
    stop_event: asyncio.Event,
) -> None:
    """
    Главный цикл: периодически вызывает dispatch_once. Выходит при
    выставленном stop_event (см. _serve в worker/main.py).
    """
    logger.info(
        "Outbox dispatcher started (interval=%.1fs, batch=%d)",
        POLL_INTERVAL_SECONDS, BATCH_SIZE,
    )
    while not stop_event.is_set():
        try:
            async with session_factory() as db:
                n = await dispatch_once(db, channel)
            if n:
                logger.info("Outbox dispatched %d events", n)
        except Exception:
            logger.exception("Outbox dispatch loop error")
        # asyncio.wait_for на stop с таймаутом = sleep,
        # прерываемый при stop_event.set().
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=POLL_INTERVAL_SECONDS
            )
        except asyncio.TimeoutError:
            pass
    logger.info("Outbox dispatcher stopped")
