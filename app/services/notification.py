"""
Сервис уведомлений (этап 9).

Назначение:
1. publish_event — публикация события в Topic Exchange. Используется
   из других сервисов (show, litter и т.д.) в "fire-and-forget" режиме.
2. Раздача уведомлений по подписчикам реализована в воркере
   (worker/handlers/events_handler.py): он подписывается на pattern,
   ищет подписчиков, формирует EmailTaskMessage и публикует в email-очередь.

Зачем разделение:
- Бизнес-сервис не должен знать про "кому и как доставлять" —
  он публикует ивент с фактом.
- Воркер событий — единая точка маршрутизации. Если завтра добавится
  push-канал, изменения локальны.
"""

from __future__ import annotations

import json
import logging

import aio_pika
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.repositories import outbox as outbox_repo
from app.schemas.notification import EventMessage
from app.services.rabbit import rabbit_service

logger = logging.getLogger(__name__)


async def publish_event(
    event: EventMessage, db: AsyncSession | None = None
) -> None:
    """
    Публикует событие. Два режима:

    1. Transactional outbox (если передан db): INSERT в outbox_events
       в текущей транзакции. Воркер outbox-dispatcher (см.
       worker/handlers/outbox_handler.py) подберёт и опубликует в Rabbit.
       Гарантия «событие в БД ⇔ бизнес-операция прошла». Это правильный
       prod-режим.

    2. Direct publish (db=None) — legacy fire-and-forget путь. При
       недоступном Rabbit событие просто теряется. Оставлен для мест,
       где сессия БД недоступна (например, фоновые задачи воркеров).

    Routing key уже в event'е — подписчики ловят его по своему pattern
    (см. events_handler).
    """
    if db is not None:
        # Transactional outbox. enqueue без COMMIT — вызывающий код
        # коммитит общую транзакцию.
        await outbox_repo.enqueue(
            db,
            exchange=settings.exchange_topic,
            routing_key=event.routing_key,
            payload=json.loads(event.to_json()),
        )
        return

    # Legacy direct-publish — fire-and-forget. На сбое Rabbit событие
    # потеряется; для критичных операций используй db-вариант.
    if rabbit_service.channel is None:
        logger.warning(
            "Cannot publish event %s: RabbitMQ not connected",
            event.event_type,
        )
        return
    try:
        exchange = await rabbit_service.channel.declare_exchange(
            settings.exchange_topic,
            aio_pika.ExchangeType.TOPIC,
            durable=True,
        )
        message = aio_pika.Message(
            body=event.to_json().encode(),
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            # content_type — для отладки в UI RabbitMQ. Не влияет
            # на consume-логику.
            content_type="application/json",
        )
        await exchange.publish(message, routing_key=event.routing_key)
        logger.info(
            "Published event %s (routing_key=%s)",
            event.event_type,
            event.routing_key,
        )
    except Exception as e:  # noqa: BLE001 — лучше залогировать чем уронить
        logger.warning(
            "Failed to publish event %s: %s", event.event_type, e
        )
