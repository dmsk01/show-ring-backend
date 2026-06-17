"""
Воркер событий (этап 9).

Подписан на Topic Exchange show-ring.events с паттерном "#" (все
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

import json
import logging
import uuid
from datetime import datetime, timezone

import aio_pika
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import redis as redis_state
from app.config import settings
from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationStatus,
)
from app.repositories import notification as notif_repo
from app.repositories import outbox as outbox_repo
from app.schemas.notification import (
    EmailTaskMessage,
    EventMessage,
    NotificationResponse,
)
from app.services.email import render_email
from app.services.rabbit_dlx import declare_workflow_queue

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


def _in_app_message_id(
    event_id: uuid.UUID, user_id: uuid.UUID
) -> uuid.UUID:
    """
    Идемпотентный ключ in_app-строки (этап 16). Отличается от email-ключа
    суффиксом ":in_app", иначе UNIQUE на message_id не дал бы вставить
    вторую строку для того же (event, user). Детерминированный → при
    redelivery того же события дубль не плодится.
    """
    return uuid.uuid5(uuid.NAMESPACE_OID, f"{event_id}:{user_id}:in_app")


def _safe_uuid(value) -> uuid.UUID | None:
    """payload.breed_id может прийти как str (JSON) — конвертируем."""
    if value is None:
        return None
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


async def _create_in_app_notification(
    db: AsyncSession,
    *,
    event: EventMessage,
    user_id: uuid.UUID,
    subject: str,
) -> dict | None:
    """
    Создаёт in_app-строку (channel=in_app, status=sent) в собственной
    транзакции и возвращает сериализованный NotificationResponse для
    WS-пуша. None — если строка уже есть (redelivery): UNIQUE на
    message_id отлавливает дубль, push не повторяем.
    """
    msg_id = _in_app_message_id(event.event_id, user_id)
    try:
        notif = Notification(
            user_id=user_id,
            event_type=event.event_type,
            channel=NotificationChannel.in_app,
            subject=subject,
            status=NotificationStatus.sent,
            sent_at=datetime.now(timezone.utc),
            message_id=msg_id,
        )
        db.add(notif)
        await db.flush()
        # refresh — подтянуть server_default created_at до сериализации
        # (NotificationResponse требует created_at непустым).
        await db.refresh(notif)
        payload = NotificationResponse.model_validate(notif).model_dump(
            mode="json"
        )
        await db.commit()
        return payload
    except IntegrityError:
        await db.rollback()
        logger.debug(
            "Skipping duplicate in_app: event=%s user=%s",
            event.event_id, user_id,
        )
        return None


async def _push_in_app(user_id: uuid.UUID, payload: dict) -> None:
    """
    Best-effort realtime-push в Redis-канал notif:{user_id}. Долетит до
    подписанных API-инстансов (notif_ws_manager._listen) и дальше в
    сокеты. Никто не подключён — Pub/Sub просто отбросит сообщение, при
    следующем GET /notifications юзер всё увидит. Redis недоступен — push
    тихо пропускается, строка в БД уже сохранена.
    """
    client = redis_state.redis_client
    if client is None:
        return
    try:
        await client.publish(
            f"notif:{user_id}",
            json.dumps({"type": "notification", "payload": payload}),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("notif push failed for user %s: %s", user_id, e)


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

    email_subs = await notif_repo.find_subscribers(
        db,
        event_type=event.event_type,
        breed_id=breed_id,
        region=region,
        channel=NotificationChannel.email,
        exclude_user_id=event.actor_id,
    )
    # Аудит M2: in_app-получателей берём по in_app-ПОДПИСКАМ, а не по email.
    # Раньше in_app создавался для каждого email-подписчика → in_app-only
    # подписчики не получали ничего, а email-подписчики форсились в in_app.
    inapp_subs = await notif_repo.find_subscribers(
        db,
        event_type=event.event_type,
        breed_id=breed_id,
        region=region,
        channel=NotificationChannel.in_app,
        exclude_user_id=event.actor_id,
    )
    if not email_subs and not inapp_subs:
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

    # ИСПРАВЛЕНО (bug_238 audit 2026-05-28): рендерим Jinja-шаблон
    # ОДИН РАЗ для всего события. Контекст (имя питомника, порода,
    # помёт) одинаковый для всех подписчиков — на 1000 подписчиков
    # повторный render тратил CPU зря. Если когда-то понадобится
    # персонализация (имя адресата в письме), вернуть render внутрь
    # цикла и передавать user-context дополнительным аргументом.
    subject, html_body, text_body = render_email(template_name, payload)

    # --- email-канал: по email-подпискам ---
    dispatched = 0
    skipped_duplicate = 0
    for _sub, user in email_subs:
        msg_id = _recipient_message_id(event.event_id, user.id)

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

    # --- in_app-канал: по in_app-подпискам (этап 16, аудит M2) ---
    # Самостоятельная строка channel=in_app + realtime-push. Свой
    # message_id, своя транзакция. SMTP не нужен → status=sent сразу.
    # Persistence (история/бейдж) отделена от push (best-effort поверх).
    pushed = 0
    for _sub, user in inapp_subs:
        in_app_payload = await _create_in_app_notification(
            db, event=event, user_id=user.id, subject=subject
        )
        if in_app_payload is not None:
            await _push_in_app(user.id, in_app_payload)
            pushed += 1

    logger.info(
        "Event %s dispatched: %d email (%d dup), %d in_app",
        event.event_type, dispatched, skipped_duplicate, pushed,
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
    # bug_239 audit 2026-05-28: workflow-очередь с DLX. Имя зашиваем,
    # чтобы очередь была одна на всех воркеров (work queue semantics:
    # каждое сообщение получает ровно один из них).
    queue = await declare_workflow_queue(channel, "show-ring.events.dispatcher")
    await queue.bind(exchange, routing_key=pattern)
    return queue
