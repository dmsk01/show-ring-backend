"""Интеграция: events_handler — развязка каналов in_app и email (аудит M2).

До фикса in_app-уведомления создавались для каждого EMAIL-подписчика:
in_app-only подписчик не получал ничего, а email-подписчик форсился в in_app.
Теперь каждый канал адресуется по своим подпискам.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import MagicMock

from sqlalchemy import select

import worker.handlers.events_handler as eh
from app.models.notification import (
    Notification,
    NotificationChannel,
    Subscription,
)
from app.models.user import User

EVENT_TYPE = "dog.title_earned"


def _event_body(actor_id: uuid.UUID | None = None) -> str:
    return json.dumps(
        {
            "event_type": EVENT_TYPE,
            "routing_key": EVENT_TYPE,
            "payload": {},
            "actor_id": str(actor_id) if actor_id else None,
        }
    )


async def _user(db_session) -> User:
    u = User(
        email=f"sub_{uuid.uuid4().hex[:8]}@example.com", hashed_password="x"
    )
    db_session.add(u)
    await db_session.commit()
    return u


async def _channels(db_session, user_id) -> set:
    rows = (
        await db_session.execute(
            select(Notification).where(Notification.user_id == user_id)
        )
    ).scalars().all()
    return {n.channel for n in rows}


async def test_in_app_decoupled_from_email_subscriptions(db_session, monkeypatch):
    # Не дёргаем Jinja-шаблоны и Redis — проверяем именно адресацию каналов.
    monkeypatch.setattr(
        eh, "render_email", lambda *a, **k: ("subj", "<p>h</p>", "t")
    )

    async def _noop_push(*a, **k):
        return None

    monkeypatch.setattr(eh, "_push_in_app", _noop_push)

    u_email = await _user(db_session)
    u_inapp = await _user(db_session)
    db_session.add_all(
        [
            Subscription(
                user_id=u_email.id, event_type=EVENT_TYPE,
                channel=NotificationChannel.email, is_active=True,
            ),
            Subscription(
                user_id=u_inapp.id, event_type=EVENT_TYPE,
                channel=NotificationChannel.in_app, is_active=True,
            ),
        ]
    )
    await db_session.commit()

    await eh.process_event(db_session, MagicMock(), _event_body())

    # email-подписчик получил ТОЛЬКО email; in_app-подписчик — ТОЛЬКО in_app.
    assert await _channels(db_session, u_email.id) == {NotificationChannel.email}
    assert await _channels(db_session, u_inapp.id) == {NotificationChannel.in_app}
