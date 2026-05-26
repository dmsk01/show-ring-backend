"""
Воркер событий (этап 9).

Подписан на Topic Exchange showtail.events с паттерном "#" (все
события). Для каждого события:
1. Парсим payload → EventMessage.
2. Извлекаем breed_id / region (если есть в payload).
3. Ищем подписчиков в БД через notif_repo.find_subscribers.
4. Для каждого формируем письмо через email.render_email.
5. Создаём Notification в БД и публикуем EmailTaskMessage в очередь
   email_tasks.

Почему отдельный воркер событий (а не сразу отправлять email):
- Email-воркер может масштабироваться независимо (несколько инстансов).
- Email-воркер ничего не знает про "подписки и фильтры" — у него
  один контракт: получил EmailTaskMessage → отправил.
- Если SMTP лёг — события не теряются (они уже доставлены подписчикам
  как pending Notification, восстановим cron-джобом).
"""

from __future__ import annotations

import logging
import uuid

import aio_pika
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.notification import NotificationChannel
from app.repositories import notification as notif_repo
from app.schemas.notification import EmailTaskMessage, EventMessage
from app.services.email import render_email

logger = logging.getLogger(__name__)


EMAIL_TASK_QUEUE = "email_tasks"


def _safe_uuid(value) -> uuid.UUID | None:
    """payload.breed_id может прийти как str (JSON) — конвертируем."""
    if value is None:
        return None
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


async def process_event(
    db: AsyncSession,
    channel: aio_pika.abc.AbstractChannel,
    body: str,
) -> None:
    """Точка входа: один event → N писем подписчикам."""
    event = EventMessage.from_json(body)
    payload = event.payload

    breed_id = _safe_uuid(payload.get("breed_id"))
    region = payload.get("region")

    subscribers = await notif_repo.find_subscribers(
        db,
        event_type=event.event_type,
        breed_id=breed_id,
        region=region,
        channel=NotificationChannel.email,
        exclude_user_id=event.actor_id,
    )
    if not subscribers:
        logger.info(
            "No subscribers for event %s (breed=%s, region=%s)",
            event.event_type,
            breed_id,
            region,
        )
        return

    # Шаблон — название = event_type. То есть для "litter.announced"
    # должен лежать файл templates/email/litter.announced.html.j2.
    template_name = event.event_type

    # Декларируем очередь email_tasks один раз. durable=True — переживёт
    # рестарт RabbitMQ; persistent сообщения уцелеют.
    queue = await channel.declare_queue(EMAIL_TASK_QUEUE, durable=True)

    for sub, user in subscribers:
        subject, html_body, text_body = render_email(template_name, payload)
        # Notification создаём ДО публикации в очередь, чтобы email-воркер
        # знал, какой статус апдейтить.
        notif = await notif_repo.create_notification(
            db,
            user_id=user.id,
            event_type=event.event_type,
            channel=NotificationChannel.email,
            subject=subject,
        )
        email_msg = EmailTaskMessage(
            notification_id=notif.id,
            to_email=user.email,
            subject=subject,
            html_body=html_body,
            text_body=text_body,
        )
        await channel.default_exchange.publish(
            aio_pika.Message(
                body=email_msg.to_json().encode(),
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                content_type="application/json",
            ),
            routing_key=queue.name,
        )
        # sub переменная используется как ссылка для возможной аналитики
        # в будущем ("какая подписка вызвала рассылку"); сейчас не нужна.
        _ = sub

    logger.info(
        "Event %s dispatched to %d subscriber(s)",
        event.event_type,
        len(subscribers),
    )


async def bind_topic_queue(
    channel: aio_pika.abc.AbstractChannel,
    pattern: str = "#",
) -> aio_pika.abc.AbstractQueue:
    """
    Создаёт именованную очередь, привязывает её к topic exchange по
    pattern. Именованная (не exclusive=True), потому что мы хотим, чтобы
    события не терялись между рестартами воркера.

    pattern="#" — все события. Если нужно слушать только нюансы
    (например, только show.*), передавайте конкретный pattern.
    """
    exchange = await channel.declare_exchange(
        settings.exchange_topic,
        aio_pika.ExchangeType.TOPIC,
        durable=True,
    )
    queue = await channel.declare_queue(
        # Имя зашиваем чтобы очередь была одна на всех воркеров (work
        # queue semantics: каждое сообщение получает ровно один из них).
        "showtail.events.dispatcher",
        durable=True,
    )
    await queue.bind(exchange, routing_key=pattern)
    return queue
