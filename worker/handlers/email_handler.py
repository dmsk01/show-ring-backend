"""
Воркер email-задач (этап 9).

Слушает очередь email_tasks. Каждое сообщение — готовое письмо
(EmailTaskMessage с subject + html). Воркер вызывает SMTP-отправку
и обновляет статус Notification в БД.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories import notification as notif_repo
from app.schemas.notification import EmailTaskMessage
from app.services.email import send_email

logger = logging.getLogger(__name__)


async def process_email_task(db: AsyncSession, body: str) -> None:
    """
    Парсит EmailTaskMessage, отправляет письмо, обновляет статус.

    Любая ошибка отправки → mark_failed с текстом исключения. RabbitMQ
    при requeue=False не отправит сообщение снова — это сознательное
    решение: стабильная ошибка (битый домен, неверный SMTP-конфиг) не
    должна крутиться в очереди вечно.
    """
    msg = EmailTaskMessage.from_json(body)
    try:
        await send_email(
            to_email=msg.to_email,
            subject=msg.subject,
            html_body=msg.html_body,
            text_body=msg.text_body,
        )
        await notif_repo.mark_sent(db, msg.notification_id)
    except Exception as e:  # noqa: BLE001
        logger.exception("Email send failed for %s", msg.to_email)
        await notif_repo.mark_failed(db, msg.notification_id, str(e))
