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
    Один проход dispatcher'а. Возвращает число событий, успешно
    отправленных (sent) в этой итерации.

    ИСПРАВЛЕНО (bug_232 audit 2026-05-28): commit ПОСЛЕ КАЖДОГО
    успешного publish. Раньше был один commit в конце пачки — если
    что-то падало посреди (publish #5 крашится с BaseException, либо
    сам mark_sent кидает DB-ошибку), то события #1..#4 уже улетели
    в RabbitMQ, а их статус в БД оставался pending → следующий тик
    выбирал и публиковал их повторно. Per-event commit ограничивает
    окно «published-но-не-marked-sent» одним событием за раз.

    Trade-off: после первого commit'а FOR UPDATE-локи на оставшихся
    строках в пачке освобождаются, и другой воркер может подхватить
    те же события. Двойной publish обрабатывается на уровне consumer'а
    (см. bug_230 — идемпотентность по message_id).
    """
    events = await outbox_repo.fetch_pending(db, limit=BATCH_SIZE)
    if not events:
        # Откатываем пустую транзакцию — иначе lock висит до timeout.
        await db.rollback()
        return 0

    sent = 0
    for ev in events:
        # Снимок полей: после rollback ORM-объект остаётся «живым»
        # благодаря expire_on_commit=False, но привычка хранить
        # значения локально защищает от изменения сценария в будущем.
        ev_id = ev.id
        ev_attempts = ev.attempts
        try:
            await _publish(publish_channel, ev)
            await outbox_repo.mark_sent(db, ev_id)
            await db.commit()
            sent += 1
        except Exception as e:  # noqa: BLE001 — изоляция падений одного события
            await db.rollback()
            logger.warning(
                "Outbox publish failed for %s (attempt %d): %s",
                ev_id, ev_attempts + 1, e,
            )
            # Бухгалтерия неудачи — отдельная транзакция, чтобы её
            # сбой не подавил предыдущий exception в логах.
            try:
                if ev_attempts + 1 >= MAX_ATTEMPTS:
                    await outbox_repo.mark_failed(db, ev_id, str(e))
                else:
                    await outbox_repo.increment_attempts(db, ev_id, str(e))
                await db.commit()
            except Exception:  # noqa: BLE001 — последний рубеж
                await db.rollback()
                logger.exception(
                    "Outbox failure-bookkeeping itself failed for %s",
                    ev_id,
                )
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
