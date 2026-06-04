"""
Адресная (транзакционная) отправка писем (этап 19).

В отличие от рассылок по подпискам (worker/handlers/events_handler.py,
который ищет N подписчиков на событие), здесь — письмо ОДНОМУ адресату:
подтверждение смены email, уведомление о смене пароля, подтверждение
регистрации. Это «системные» письма, а не уведомления по подписке.

Механика та же, что у events_handler.process_event, но без цикла по
подписчикам:
1. рендерим готовое письмо через render_email;
2. создаём Notification (лог + идемпотентность по message_id);
3. кладём EmailTaskMessage в outbox (та же транзакция) — outbox-dispatcher
   вытолкнет в очередь email_tasks, email_handler отправит по SMTP.

Почему через outbox, а не прямой publish: гарантия «письмо
зарегистрировано ⇔ бизнес-операция закоммичена». Вызывающий код
коммитит общую транзакцию сам (enqueue без commit).
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationStatus,
)
from app.repositories import outbox as outbox_repo
from app.schemas.notification import EmailTaskMessage
from app.services.email import render_email

logger = logging.getLogger(__name__)

EMAIL_TASK_QUEUE = "email_tasks"


async def enqueue_transactional_email(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    to_email: str,
    template_name: str,
    context: dict,
) -> None:
    """
    Зарегистрировать адресное письмо в текущей транзакции.

    Параметры:
    - user_id    — кому (для лога Notification; адрес может отличаться от
                   users.email, например новый pending_email).
    - to_email   — фактический адрес доставки.
    - template_name — имя шаблона в app/templates/email/ (без .html.j2).
    - context    — переменные шаблона (confirm_url, new_email и т.п.).

    НЕ коммитит — вызывающий код коммитит транзакцию вместе с основной
    операцией (запись pending_email, смена пароля). message_id —
    свежий uuid4: каждое транзакционное письмо уникально (в отличие от
    рассылок, где message_id детерминирован по event+user).
    """
    subject, html_body, text_body = render_email(template_name, context)
    message_id = uuid.uuid4()

    notif = Notification(
        user_id=user_id,
        event_type=f"transactional.{template_name}",
        channel=NotificationChannel.email,
        subject=subject,
        status=NotificationStatus.pending,
        message_id=message_id,
    )
    db.add(notif)
    await db.flush()  # нужен notif.id для payload

    email_msg = EmailTaskMessage(
        notification_id=notif.id,
        message_id=message_id,
        to_email=to_email,
        subject=subject,
        html_body=html_body,
        text_body=text_body,
    )
    await outbox_repo.enqueue(
        db,
        exchange=None,  # default exchange → routing_key = имя очереди
        routing_key=EMAIL_TASK_QUEUE,
        payload=email_msg.model_dump(mode="json"),
    )
    logger.info(
        "Transactional email queued: template=%s user=%s",
        template_name,
        user_id,
    )
