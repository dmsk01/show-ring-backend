# tests/integration/test_checkin_reminders.py
"""Интеграция: напоминание о недостающих документах за 3 дня до выставки."""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select

from app.models.notification import Notification, NotificationChannel
from app.models.show import ShowStatus
from app.services import checkin_reminders as rem
from tests.integration.checkin_helpers import add_entry, make_world

TODAY = date(2026, 10, 4)


async def _notifs(db_session, user_id):
    rows = await db_session.execute(select(Notification).where(Notification.user_id == user_id))
    return list(rows.scalars())


async def test_collect_groups_by_recipient(db_session):
    w = await make_world(db_session, date_start=TODAY + timedelta(days=3),
                         status=ShowStatus.registration_open)
    await add_entry(db_session, w, owner=w.owner, name="Вторая")
    reminders = [r for r in await rem.collect_document_reminders(db_session, TODAY) if r.show.id == w.show.id]
    assert len(reminders) == 1
    assert reminders[0].user_id == w.owner.id
    assert len(reminders[0].items) == 2


async def test_skips_other_dates_and_disabled(db_session):
    a = await make_world(db_session, date_start=TODAY + timedelta(days=4))
    b = await make_world(db_session, date_start=TODAY + timedelta(days=3), checkin_enabled=False)
    show_ids = {r.show.id for r in await rem.collect_document_reminders(db_session, TODAY)}
    assert a.show.id not in show_ids and b.show.id not in show_ids


async def test_send_is_idempotent(db_session, monkeypatch):
    monkeypatch.setattr(rem, "render_email", lambda name, ctx: ("Документы", "<p>h</p>", "t"))
    w = await make_world(db_session, date_start=TODAY + timedelta(days=3))
    await rem.send_document_reminders(db_session, TODAY)
    first = await _notifs(db_session, w.owner.id)
    assert {n.channel for n in first} == {NotificationChannel.in_app, NotificationChannel.email}
    await rem.send_document_reminders(db_session, TODAY)
    assert len(await _notifs(db_session, w.owner.id)) == len(first)
