"""
Точка входа фонового воркера.

Запуск:
    python -m worker.main                   # обработка documents-очереди (этап 8)
    python -m worker.main --mode book       # учебная очередь "tasks" (book_handler)
    python -m worker.main --mode events     # подписка на fanout events

Что внутри:
- aio-pika подключается к RabbitMQ через connect_robust — это
  переподключение при разрыве сети без падения воркера.
- prefetch_count=1: воркер забирает по одному сообщению, чтобы тяжёлый
  PDF-рендер не блокировал остальные.
- Каждое сообщение обрабатывается в собственной транзакции БД.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal

import aio_pika

from app.config import settings
from app.database import async_session_factory
from worker.handlers.ad_handler import (
    AD_EVENTS_QUEUE,
    init_accumulator,
    on_ad_event_message,
)
from worker.handlers.book_handler import process_book
from worker.handlers.document_handler import process_document_task
from worker.handlers.email_handler import process_email_task
from worker.handlers.events_handler import (
    EMAIL_TASK_QUEUE,
    bind_topic_queue,
    process_event,
)
from worker.handlers.outbox_handler import run_loop as outbox_run_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("worker")


# Имя очереди задач документов. Должно совпадать с константой в
# app/routers/documents.py — публикатор и подписчик читают/пишут одну
# и ту же. Дублирование констант временно: на этапе 14 общие настройки
# уедут в app.config.
DOCUMENT_TASK_QUEUE = "document_task"


# ---------------------------------------------------------------------
# Document handler bridge
# ---------------------------------------------------------------------


async def on_document_message(message: aio_pika.abc.AbstractIncomingMessage):
    """
    Обёртка вокруг process_document_task: парсит тело, создаёт сессию БД,
    вызывает хендлер. Если хендлер бросает исключение наружу, ack не
    делается и RabbitMQ переотправит сообщение позднее.
    """
    async with message.process(requeue=False):
        # requeue=False: при ошибке сообщение НЕ возвращается в очередь
        # автоматически. Хендлер сам обновляет task.status='failed', и
        # клиент видит результат. Иначе задача крутилась бы в очереди
        # бесконечно при стабильной ошибке (например, битый payload).
        body = message.body.decode()
        try:
            data = json.loads(body)
            task_id = data["task_id"]
        except (json.JSONDecodeError, KeyError) as e:
            logger.error("Bad document task message: %s (%s)", body, e)
            return

        async with async_session_factory() as db:
            try:
                # task_id приходит как строка UUID — переводим тут, чтобы
                # хендлер работал с типизированным значением.
                import uuid as _uuid

                tid = _uuid.UUID(task_id)
                await process_document_task(db, tid)
            except Exception:
                logger.exception("Document task %s failed", task_id)


# ---------------------------------------------------------------------
# Legacy: учебный book_task
# ---------------------------------------------------------------------


async def on_book_message(message: aio_pika.abc.AbstractIncomingMessage):
    async with message.process():
        body = message.body.decode()
        logger.info("book_task message: %s", body)
        data = json.loads(body)
        await process_book(data["task_id"], data["payload"])


# ---------------------------------------------------------------------
# Fanout events
# ---------------------------------------------------------------------


async def on_event(message: aio_pika.abc.AbstractIncomingMessage):
    async with message.process():
        logger.info("event: %s", message.body.decode())


# ---------------------------------------------------------------------
# Graceful shutdown helpers
# ---------------------------------------------------------------------


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    """
    SIGTERM/SIGINT → stop_event.set(). После этого основной цикл выходит
    из await, закрывает соединение, и aio-pika корректно ack'ает уже
    взятое сообщение (message.process() doesn't accept new messages
    после close).

    add_signal_handler работает только на Unix. На Windows бросает
    NotImplementedError — там полагаемся на KeyboardInterrupt
    (signal.SIGINT через стандартный механизм Python).
    """
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            # Windows: add_signal_handler не поддерживается. Ctrl+C
            # всё равно прерывает через KeyboardInterrupt в main loop.
            logger.debug("Signal handler not supported for %s", sig)


async def _serve(
    connection: aio_pika.abc.AbstractRobustConnection,
    queue_name: str,
) -> None:
    """
    Общий "main loop" для всех режимов воркера: ждёт SIGTERM/SIGINT,
    после чего закрывает соединение. Внутри `message.process()` aio-pika
    уже взявшее сообщение ack'нет в финале — мы не теряем данные.
    """
    stop = asyncio.Event()
    _install_signal_handlers(stop)
    try:
        logger.info("Listening on '%s'. Send SIGTERM to stop gracefully.", queue_name)
        await stop.wait()
        logger.info("Shutdown signal received, draining in-flight messages…")
    finally:
        # close() ждёт текущие consume-задачи. В aio-pika это означает:
        # уже запущенные обработчики дойдут до конца, новые сообщения
        # не примутся.
        await connection.close()
        logger.info("Worker stopped")


# ---------------------------------------------------------------------
# Main loops
# ---------------------------------------------------------------------


async def run_documents() -> None:
    connection = await aio_pika.connect_robust(settings.rabbitmq_url)
    channel = await connection.channel()
    # prefetch_count=1 — воркер не забирает следующее сообщение, пока
    # не обработал текущее. На PDF-рендере это критично: тяжёлая задача
    # не должна "связывать" десяток сообщений из-под себя.
    await channel.set_qos(prefetch_count=1)
    queue = await channel.declare_queue(DOCUMENT_TASK_QUEUE, durable=True)
    await queue.consume(on_document_message)
    await _serve(connection, DOCUMENT_TASK_QUEUE)


async def run_book() -> None:
    connection = await aio_pika.connect_robust(settings.rabbitmq_url)
    channel = await connection.channel()
    await channel.set_qos(prefetch_count=1)
    queue = await channel.declare_queue("tasks", durable=True)
    await queue.consume(on_book_message)
    await _serve(connection, "tasks (legacy book worker)")


async def run_events() -> None:
    """Legacy fanout-режим (учебный пример, не используется в этапе 9)."""
    connection = await aio_pika.connect_robust(settings.rabbitmq_url)
    channel = await connection.channel()
    exchange = await channel.declare_exchange(
        "events", aio_pika.ExchangeType.FANOUT, durable=True
    )
    queue = await channel.declare_queue("", exclusive=True, auto_delete=True)
    await queue.bind(exchange)
    await queue.consume(on_event)
    await _serve(connection, "events (fanout)")


# ---------------------------------------------------------------------
# Этап 9: events dispatcher + email worker
# ---------------------------------------------------------------------


# Глобальный publish-канал на воркер событий. Открывается один раз
# в run_topic_events и переиспользуется. message.channel здесь не
# подходит: возвращает aiormq-уровневый канал, несовместимый по типам
# с aio_pika.abc.AbstractChannel, которого ожидает process_event.
_topic_publish_channel: aio_pika.abc.AbstractChannel | None = None


async def on_topic_event(message: aio_pika.abc.AbstractIncomingMessage):
    """
    Получает event из topic exchange, формирует email_tasks через
    events_handler. requeue=False — стабильная ошибка не зацикливает
    обработку.
    """
    async with message.process(requeue=False):
        body = message.body.decode()
        if _topic_publish_channel is None:
            logger.error("Topic worker not initialised — channel is None")
            return
        async with async_session_factory() as db:
            try:
                await process_event(db, _topic_publish_channel, body)
            except Exception:
                logger.exception("Event processing failed: %s", body)


async def on_email_task(message: aio_pika.abc.AbstractIncomingMessage):
    async with message.process(requeue=False):
        body = message.body.decode()
        async with async_session_factory() as db:
            try:
                await process_email_task(db, body)
            except Exception:
                logger.exception("Email task failed: %s", body)


async def run_topic_events() -> None:
    """
    Воркер событий: подписан на topic exchange и распределяет события
    подписчикам. Это "роутер событий", не отправитель писем.

    Используем ДВА отдельных канала: один для consume (с prefetch_count),
    второй (глобальный _topic_publish_channel) для publish email-task'ов
    в очередь. По best practice RabbitMQ — один канал не должен
    одновременно делать consume и большой объём publish.
    """
    global _topic_publish_channel

    connection = await aio_pika.connect_robust(settings.rabbitmq_url)
    consume_ch = await connection.channel()
    # prefetch=10: одно событие может породить много writes (Notification
    # × N подписчиков). Большой prefetch ускоряет throughput при низкой
    # нагрузке per-event.
    await consume_ch.set_qos(prefetch_count=10)

    _topic_publish_channel = await connection.channel()

    queue = await bind_topic_queue(consume_ch, pattern="#")
    await queue.consume(on_topic_event)
    await _serve(connection, f"topic '{settings.exchange_topic}' (#)")


async def on_ad_event(message: aio_pika.abc.AbstractIncomingMessage):
    """Обёртка вокруг ad_handler.on_ad_event_message с правильным ack."""
    async with message.process(requeue=False):
        body = message.body.decode()
        try:
            await on_ad_event_message(body)
        except Exception:
            logger.exception("ad_event processing failed: %s", body)


async def run_ad_events() -> None:
    """
    Воркер событий рекламы (этап 14): подписан на ad_events, копит
    события в батч и пишет в БД одним INSERT'ом. См.
    worker/handlers/ad_handler.py.
    """
    connection = await aio_pika.connect_robust(settings.rabbitmq_url)
    channel = await connection.channel()
    # prefetch=100: события мелкие, можем тянуть пачку — внутри
    # accumulator всё равно батчит по своему окну.
    await channel.set_qos(prefetch_count=100)
    queue = await channel.declare_queue(AD_EVENTS_QUEUE, durable=True)
    # Стартуем аккумулятор: периодический flush уходит в фоновую задачу.
    accumulator = init_accumulator(async_session_factory)
    await queue.consume(on_ad_event)
    # bug_235 audit 2026-05-28: после _serve (он закрывает connection
    # и дожидается завершения in-flight consume-tasks) явно
    # останавливаем accumulator. Иначе фоновый _periodic_flush был бы
    # убит вместе с event loop'ом, и батч в памяти терялся бы.
    try:
        await _serve(connection, f"{AD_EVENTS_QUEUE} (ads batch)")
    finally:
        await accumulator.stop()


async def run_outbox() -> None:
    """
    Outbox dispatcher (follow-up для этапов 9/10). Опрашивает таблицу
    outbox_events и публикует pending события в RabbitMQ. См.
    worker/handlers/outbox_handler.py.

    В отличие от других режимов, тут нет consume-подписки на очередь —
    воркер сам читает БД. Поэтому _serve не подходит: используем
    свой stop_event и run_loop.
    """
    connection = await aio_pika.connect_robust(settings.rabbitmq_url)
    channel = await connection.channel()

    stop = asyncio.Event()
    _install_signal_handlers(stop)
    try:
        await outbox_run_loop(async_session_factory, channel, stop)
    finally:
        await connection.close()


async def run_email() -> None:
    """
    Воркер отправки email. Простой: один тип сообщений
    (EmailTaskMessage), один транспорт (SMTP).
    """
    connection = await aio_pika.connect_robust(settings.rabbitmq_url)
    channel = await connection.channel()
    # prefetch=1: SMTP-серверы любят throttling. Один-в-один — спокойный
    # ритм. На большом трафике добавляются параллельные воркеры, а не
    # увеличивается prefetch.
    await channel.set_qos(prefetch_count=1)
    queue = await channel.declare_queue(EMAIL_TASK_QUEUE, durable=True)
    await queue.consume(on_email_task)
    await _serve(connection, f"{EMAIL_TASK_QUEUE} (SMTP sender)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=["documents", "book", "events", "topic", "email", "ads", "outbox"],
        default="documents",
        help=(
            "documents — PDF-задачи; book — учебный пример; "
            "events — fanout-демо; topic — диспатчер событий этапа 9; "
            "email — SMTP-воркер этапа 9; ads — batch-воркер рекламных "
            "событий этапа 14; outbox — dispatcher outbox_events → "
            "RabbitMQ (transactional outbox)."
        ),
    )
    args = parser.parse_args()
    coro = {
        "documents": run_documents,
        "book": run_book,
        "events": run_events,
        "topic": run_topic_events,
        "email": run_email,
        "ads": run_ad_events,
        "outbox": run_outbox,
    }[args.mode]
    asyncio.run(coro())


if __name__ == "__main__":
    main()
