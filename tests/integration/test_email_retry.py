"""
Интеграция: повторная отправка писем при временных ошибках SMTP
(ревью 2026-10-06, BE-20).

Раньше любая ошибка SMTP — даже обрыв соединения или 4xx «попробуйте
позже» — сразу помечала уведомление failed, и письмо (подтверждение email,
смена почты, блокировка аккаунта) терялось. Теперь:
- временная ошибка → повтор через outbox с задержкой (locked_until),
  уведомление остаётся pending;
- постоянная (5xx, адрес отклонён) или исчерпаны попытки → failed.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import aiosmtplib
from sqlalchemy import select

from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationStatus,
)
from app.models.outbox import OutboxEvent
from app.schemas.notification import EmailTaskMessage
from tests.integration.checkin_helpers import make_db_user
from worker.handlers import email_handler


async def _notification(db_session) -> Notification:
    user = await make_db_user(db_session)
    notif = Notification(
        user_id=user.id, event_type="test", channel=NotificationChannel.email,
        subject="s", status=NotificationStatus.pending, message_id=uuid.uuid4(),
    )
    db_session.add(notif)
    await db_session.commit()
    return notif


def _body(notif: Notification, attempt: int = 0) -> str:
    return EmailTaskMessage(
        notification_id=notif.id, message_id=notif.message_id,
        to_email="user@example.com", subject="s", html_body="<p>x</p>",
        attempt=attempt,
    ).to_json()


async def _retries_for(db_session, notif_id) -> list[OutboxEvent]:
    rows = (await db_session.execute(select(OutboxEvent))).scalars().all()
    return [r for r in rows if r.payload.get("notification_id") == str(notif_id)]


async def test_transient_error_schedules_delayed_retry(db_session, monkeypatch):
    notif = await _notification(db_session)
    monkeypatch.setattr(
        email_handler, "send_email",
        AsyncMock(side_effect=aiosmtplib.SMTPServerDisconnected("bye")),
    )
    await email_handler.process_email_task(db_session, _body(notif))

    await db_session.refresh(notif)
    assert notif.status == NotificationStatus.pending
    (retry,) = await _retries_for(db_session, notif.id)
    assert retry.payload["attempt"] == 1
    assert retry.locked_until is not None
    assert retry.locked_until > datetime.now(timezone.utc)


async def test_permanent_error_marks_failed(db_session, monkeypatch):
    notif = await _notification(db_session)
    monkeypatch.setattr(
        email_handler, "send_email",
        AsyncMock(side_effect=aiosmtplib.SMTPRecipientsRefused([])),
    )
    await email_handler.process_email_task(db_session, _body(notif))
    await db_session.refresh(notif)
    assert notif.status == NotificationStatus.failed
    assert await _retries_for(db_session, notif.id) == []


async def test_transient_error_after_last_attempt_marks_failed(db_session, monkeypatch):
    notif = await _notification(db_session)
    monkeypatch.setattr(
        email_handler, "send_email",
        AsyncMock(side_effect=aiosmtplib.SMTPServerDisconnected("bye")),
    )
    last = len(email_handler.RETRY_DELAYS)
    await email_handler.process_email_task(db_session, _body(notif, attempt=last))
    await db_session.refresh(notif)
    assert notif.status == NotificationStatus.failed
    assert await _retries_for(db_session, notif.id) == []
