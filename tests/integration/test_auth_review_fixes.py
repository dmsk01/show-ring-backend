"""
Интеграция: правки по review 2026-06-10 — auth.

- При запросе смены email информационное письмо уходит и на СТАРЫЙ адрес
  (стандартная практика: атакующий с украденным паролем не должен иметь
  возможность тихо перевесить аккаунт на свою почту; письмо на новый
  адрес владельцу-жертве ничего не скажет).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.models.outbox import OutboxEvent
from app.services.email_tasks import EMAIL_TASK_QUEUE

PASSWORD = "secret123"


async def test_email_change_notifies_old_address(client, db_session):
    old_email = f"old_{uuid.uuid4().hex[:10]}@example.com"
    new_email = f"new_{uuid.uuid4().hex[:10]}@example.com"
    await client.post(
        "/auth/register", json={"email": old_email, "password": PASSWORD}
    )
    r = await client.post(
        "/auth/login", json={"email": old_email, "password": PASSWORD}
    )
    token = r.json()["access_token"]

    # Снимок ДО запроса: регистрация уже положила verify-письмо на старый
    # адрес — без диффа тест проходил бы и без правки.
    before_ids = {
        row.id
        for row in (
            await db_session.execute(
                select(OutboxEvent).where(
                    OutboxEvent.routing_key == EMAIL_TASK_QUEUE
                )
            )
        ).scalars()
    }

    r = await client.put(
        "/users/me",
        json={"email": new_email, "current_password": PASSWORD},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200

    rows = (
        await db_session.execute(
            select(OutboxEvent).where(
                OutboxEvent.routing_key == EMAIL_TASK_QUEUE
            )
        )
    ).scalars().all()
    recipients = {
        row.payload.get("to_email") for row in rows if row.id not in before_ids
    }
    # Письмо-подтверждение на новый адрес — как раньше.
    assert new_email in recipients
    # И уведомление на старый адрес — собственно правка.
    assert old_email in recipients, (
        "старый адрес не уведомлён о запросе смены email"
    )
