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
from app.services.rabbit_dlx import declare_workflow_queue

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
) -> tuple[int, int]:
    """
    Один проход dispatcher'а. Возвращает (sent, failed):
    - sent — число событий, успешно опубликованных,
    - failed — число событий, для которых publish бросил
      исключение (помечены `pending+attempts++` или `failed` если
      attempts достиг лимита).

    bug_237 audit 2026-05-28: run_loop использует пару (sent, failed)
    для экспоненциального backoff'а — если N итераций подряд имеют
    sent==0 при failed>0, sleep растёт, чтобы не молотить БД и
    лог-агрегатор при лежачем Rabbit'е.

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
        return 0, 0

    sent = 0
    failed = 0
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
            failed += 1
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
    return sent, failed


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
        # bug_239 audit 2026-05-28: workflow-очередь с DLX-аргументами —
        # outbox-publisher должен видеть те же declare-параметры, что
        # и consumer'ы (иначе PRECONDITION_FAILED при mismatch).
        await declare_workflow_queue(channel, event.routing_key)
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

    ИСПРАВЛЕНО (bug_237 audit 2026-05-28): экспоненциальный backoff
    при сбоях. Раньше при недоступном Rabbit каждые 2 сек логировался
    failed publish — за час 1800 одинаковых warning'ов засоряли лог-
    агрегатор и грели CPU/DB зря. Теперь интервал sleep'а растёт
    2s→4s→8s→16s→32s→60s (cap) при подряд идущих ошибках; на первой
    успешной итерации (sent>0 или пустая outbox-таблица) сбрасывается
    в базовый POLL_INTERVAL_SECONDS.
    """
    BACKOFF_CAP = 60.0
    backoff = POLL_INTERVAL_SECONDS
    logger.info(
        "Outbox dispatcher started (interval=%.1fs, batch=%d)",
        POLL_INTERVAL_SECONDS, BATCH_SIZE,
    )
    while not stop_event.is_set():
        sent = 0
        failed = 0
        try:
            async with session_factory() as db:
                sent, failed = await dispatch_once(db, channel)
            if sent:
                logger.info("Outbox dispatched %d events", sent)
        except Exception:
            logger.exception("Outbox dispatch loop error")
            # Loop-уровневая ошибка (БД/Redis/etc) — растим backoff
            # так же, как при failed-publish'ах.
            backoff = min(backoff * 2, BACKOFF_CAP)
        else:
            # Условие «прогресс был»: либо что-то отправлено, либо
            # outbox пуст (failed==0). Сбрасываем backoff.
            if failed == 0 or sent > 0:
                backoff = POLL_INTERVAL_SECONDS
            else:
                # Только failed'ы (Rabbit лежит, например) — растим.
                backoff = min(backoff * 2, BACKOFF_CAP)
        # asyncio.wait_for на stop с таймаутом = sleep,
        # прерываемый при stop_event.set().
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=backoff
            )
        except asyncio.TimeoutError:
            pass
    logger.info("Outbox dispatcher stopped")
