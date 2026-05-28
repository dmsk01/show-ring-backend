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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationStatus,
)
from app.repositories import notification as notif_repo
from app.repositories import outbox as outbox_repo
from app.schemas.notification import EmailTaskMessage, EventMessage
from app.services.email import render_email

logger = logging.getLogger(__name__)


EMAIL_TASK_QUEUE = "email_tasks"


def _recipient_message_id(
    event_id: uuid.UUID, user_id: uuid.UUID
) -> uuid.UUID:
    """
    bug_230 audit 2026-05-28: детерминированный idempotency-ключ на
    пару (event, user). uuid5(NAMESPACE_OID, "<event_id>:<user_id>")
    выдаёт один и тот же uuid при любом числе повторных вызовов —
    идеально для UNIQUE-индекса. NAMESPACE_OID — стандартный namespace
    из RFC 4122; выбор не принципиален, важна стабильность.
    """
    return uuid.uuid5(uuid.NAMESPACE_OID, f"{event_id}:{user_id}")


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
    """
    Точка входа: один event → N писем подписчикам.

    ИСПРАВЛЕНО (bug_231 audit 2026-05-28): раньше Notification
    коммитился, потом отдельным шагом публиковался email_task в Rabbit.
    При краше воркера между commit и publish уведомление висело pending
    навсегда — письмо не уходило. Теперь Notification и outbox-event
    кладутся в БД в одной транзакции; outbox-publisher выталкивает
    email_task в очередь с гарантией at-least-once.

    ИСПРАВЛЕНО (bug_230 audit 2026-05-28): per-recipient message_id
    (uuid5 от event_id+user_id) с UNIQUE-constraint защищает от
    дублей при redelivery события. На второй обработке INSERT падает
    с IntegrityError → подписчик пропускается; events_handler
    идемпотентен.

    Параметр `channel` оставлен в подписи ради обратной совместимости
    с worker/main.py — теперь не используется (publish идёт через
    outbox dispatcher). Удаление — отдельный рефакторинг.
    """
    _ = channel  # сохраняем сигнатуру; publish теперь через outbox

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

    dispatched = 0
    skipped_duplicate = 0
    for sub, user in subscribers:
        msg_id = _recipient_message_id(event.event_id, user.id)
        subject, html_body, text_body = render_email(template_name, payload)

        # Per-subscriber commit. Если краш между двумя подписчиками,
        # уже зафиксированные не теряются, а необработанные подберутся
        # при redelivery RabbitMQ — UNIQUE на message_id защитит уже
        # отправленных от повторного создания.
        try:
            notif = Notification(
                user_id=user.id,
                event_type=event.event_type,
                channel=NotificationChannel.email,
                subject=subject,
                status=NotificationStatus.pending,
                message_id=msg_id,
            )
            db.add(notif)
            await db.flush()  # вытащить notif.id для outbox payload

            email_msg = EmailTaskMessage(
                notification_id=notif.id,
                message_id=msg_id,
                to_email=user.email,
                subject=subject,
                html_body=html_body,
                text_body=text_body,
            )
            # bug_231: outbox-enqueue в той же транзакции, что и
            # Notification → атомарность «уведомление создано ⇔
            # задача на отправку зарегистрирована».
            await outbox_repo.enqueue(
                db,
                exchange=None,  # default exchange → routing_key=queue
                routing_key=EMAIL_TASK_QUEUE,
                payload=email_msg.model_dump(mode="json"),
            )
            await db.commit()
            dispatched += 1
        except IntegrityError:
            # bug_230: UNIQUE на message_id поймал повторную обработку
            # этого же (event, user). Это нормальный путь redelivery —
            # просто пропускаем, не считаем за ошибку.
            await db.rollback()
            skipped_duplicate += 1
            logger.debug(
                "Skipping duplicate notification: event=%s user=%s",
                event.event_id, user.id,
            )
        # sub переменная не используется — на будущее (аналитика
        # «какая подписка вызвала рассылку»).
        _ = sub

    logger.info(
        "Event %s dispatched: %d new, %d duplicate-skipped",
        event.event_type, dispatched, skipped_duplicate,
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
