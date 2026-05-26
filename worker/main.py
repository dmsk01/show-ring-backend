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

import aio_pika

from app.config import settings
from app.database import async_session_factory
from worker.handlers.book_handler import process_book
from worker.handlers.document_handler import process_document_task
from worker.handlers.email_handler import process_email_task
from worker.handlers.events_handler import (
    EMAIL_TASK_QUEUE,
    bind_topic_queue,
    process_event,
)

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
    logger.info("Listening on %s. Press Ctrl+C to exit.", DOCUMENT_TASK_QUEUE)
    try:
        await asyncio.Future()
    finally:
        await connection.close()


async def run_book() -> None:
    connection = await aio_pika.connect_robust(settings.rabbitmq_url)
    channel = await connection.channel()
    await channel.set_qos(prefetch_count=1)
    queue = await channel.declare_queue("tasks", durable=True)
    await queue.consume(on_book_message)
    logger.info("Listening on 'tasks' (legacy book worker)")
    try:
        await asyncio.Future()
    finally:
        await connection.close()


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
    logger.info("Subscribed to fanout 'events'")
    try:
        await asyncio.Future()
    finally:
        await connection.close()


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
    logger.info(
        "Listening on topic exchange '%s' (pattern '#')",
        settings.exchange_topic,
    )
    try:
        await asyncio.Future()
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
    logger.info("Listening on '%s' (SMTP sender)", EMAIL_TASK_QUEUE)
    try:
        await asyncio.Future()
    finally:
        await connection.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=["documents", "book", "events", "topic", "email"],
        default="documents",
        help=(
            "documents — PDF-задачи; book — учебный пример; "
            "events — fanout-демо; topic — диспатчер событий этапа 9; "
            "email — SMTP-воркер этапа 9."
        ),
    )
    args = parser.parse_args()
    coro = {
        "documents": run_documents,
        "book": run_book,
        "events": run_events,
        "topic": run_topic_events,
        "email": run_email,
    }[args.mode]
    asyncio.run(coro())


if __name__ == "__main__":
    main()
