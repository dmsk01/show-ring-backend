"""
Воркер email-задач (этап 9).

Слушает очередь email_tasks. Каждое сообщение — готовое письмо
(EmailTaskMessage с subject + html). Воркер вызывает SMTP-отправку
и обновляет статус Notification в БД.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import aiosmtplib
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import Notification, NotificationStatus
from app.repositories import notification as notif_repo
from app.repositories import outbox as outbox_repo
from app.schemas.notification import EmailTaskMessage
from app.services.email import send_email
from app.utils.log_mask import mask_email

logger = logging.getLogger(__name__)

EMAIL_TASK_QUEUE = "email_tasks"

# Задержки повторов при временных ошибках SMTP (ревью 2026-10-06, BE-20).
# После последней — failed. Суммарно ~36 минут: переживает перезапуск
# SMTP-сервера и кратковременную сетевую проблему.
RETRY_DELAYS = (
    timedelta(minutes=1),
    timedelta(minutes=5),
    timedelta(minutes=30),
)


def _is_transient(exc: Exception) -> bool:
    """Временная ли ошибка SMTP (стоит повторить) или постоянная.

    4xx-ответы сервера, обрыв/таймаут соединения — временные; отказ
    получателя/отправителя и 5xx — постоянные (повтор не поможет).
    """
    if isinstance(exc, (aiosmtplib.SMTPRecipientsRefused, aiosmtplib.SMTPSenderRefused)):
        return False
    if isinstance(exc, aiosmtplib.SMTPResponseException):
        return 400 <= exc.code < 500
    return isinstance(
        exc,
        (
            aiosmtplib.SMTPConnectError,
            aiosmtplib.SMTPServerDisconnected,
            aiosmtplib.SMTPTimeoutError,
            asyncio.TimeoutError,
            OSError,
        ),
    )


async def process_email_task(db: AsyncSession, body: str) -> None:
    """
    Парсит EmailTaskMessage, отправляет письмо, обновляет статус.

    Любая ошибка отправки → mark_failed с текстом исключения. RabbitMQ
    при requeue=False не отправит сообщение снова — это сознательное
    решение: стабильная ошибка (битый домен, неверный SMTP-конфиг) не
    должна крутиться в очереди вечно.

    ИСПРАВЛЕНО (bug_230 audit 2026-05-28): перед send_email
    проверяем статус Notification. Если уведомление уже sent — это
    повторная доставка RabbitMQ (worker крашнулся после send_email,
    но до ack, либо outbox-publisher продублировал). Тихо завершаем —
    письмо ушло один раз, дубля не будет.
    """
    msg = EmailTaskMessage.from_json(body)

    notif = await db.get(Notification, msg.notification_id)
    if notif is None:
        # Notification удалили (cleanup?) или ID битый — нечего апдейтить.
        logger.warning(
            "Email task references unknown notification %s — skipping",
            msg.notification_id,
        )
        return
    if notif.status == NotificationStatus.sent:
        logger.info(
            "Notification %s already sent — idempotent skip",
            msg.notification_id,
        )
        return

    try:
        await send_email(
            to_email=msg.to_email,
            subject=msg.subject,
            html_body=msg.html_body,
            text_body=msg.text_body,
        )
        await notif_repo.mark_sent(db, msg.notification_id)
    except Exception as e:  # noqa: BLE001
        if _is_transient(e) and msg.attempt < len(RETRY_DELAYS):
            delay = RETRY_DELAYS[msg.attempt]
            logger.warning(
                "Email send transient failure for notification %s (%s), "
                "retry %d in %s: %s",
                msg.notification_id, mask_email(msg.to_email),
                msg.attempt + 1, delay, e,
            )
            retry = msg.model_copy(update={"attempt": msg.attempt + 1})
            await outbox_repo.enqueue(
                db,
                exchange=None,
                routing_key=EMAIL_TASK_QUEUE,
                payload=retry.model_dump(mode="json"),
                delay=delay,
            )
            await db.commit()
            return
        logger.exception(
            "Email send failed for notification %s (%s)",
            msg.notification_id, mask_email(msg.to_email),
        )
        await notif_repo.mark_failed(db, msg.notification_id, str(e))
