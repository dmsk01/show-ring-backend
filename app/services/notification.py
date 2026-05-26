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

import logging

import aio_pika

from app.config import settings
from app.schemas.notification import EventMessage
from app.services.rabbit import rabbit_service

logger = logging.getLogger(__name__)


async def publish_event(event: EventMessage) -> None:
    """
    Публикует событие в Topic Exchange. Routing key уже в event'е —
    подписчики ловят его по своему pattern (см. events_handler).

    Если RabbitMQ недоступен, не падаем — просто логируем warning.
    Бизнес-операция (создание помёта, открытие регистрации) не должна
    откатываться из-за того, что email-канал лежит.

    fire-and-forget: вызывающий код не ждёт результата, событие
    "ушло — и забыли".
    """
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
