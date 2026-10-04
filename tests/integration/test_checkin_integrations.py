# tests/integration/test_checkin_integrations.py
"""Интеграция чек-ина с выставкой и результатами."""

from __future__ import annotations

import pytest

from app.models.show import AttendanceStatus, ShowStatus
from app.services import result as result_svc
from app.services import show as show_svc
from tests.integration.checkin_helpers import auth, make_api_user, make_world


async def test_absent_marked_on_start(db_session):
    w = await make_world(db_session)
    await show_svc.change_status(db_session, w.show.id, w.organizer.id, False, ShowStatus.in_progress)
    await db_session.refresh(w.entry)
    assert w.entry.attendance_status == AttendanceStatus.absent
    assert w.entry.attendance_changed_at is not None


async def test_no_absent_when_checkin_disabled(db_session):
    w = await make_world(db_session, checkin_enabled=False)
    await show_svc.change_status(db_session, w.show.id, w.organizer.id, False, ShowStatus.in_progress)
    await db_session.refresh(w.entry)
    assert w.entry.attendance_status == AttendanceStatus.registered
    # Результат вносится как раньше.
    res = await result_svc.upsert_class_result(
        db_session, show_entry_id=w.entry.id, user_id=w.organizer.id, is_admin=False,
        grade_id=None, placement=None, critique=None,
    )
    assert res is not None


async def test_results_blocked_for_absent_and_rejected(db_session):
    w = await make_world(db_session, status=ShowStatus.in_progress)
    for st in (AttendanceStatus.absent, AttendanceStatus.rejected):
        w.entry.attendance_status = st
        await db_session.commit()
        with pytest.raises(ValueError, match="entry_not_admitted"):
            await result_svc.upsert_class_result(
                db_session, show_entry_id=w.entry.id, user_id=w.organizer.id,
                is_admin=False, grade_id=None, placement=None, critique=None,
            )
    w.entry.attendance_status = AttendanceStatus.registered
    await db_session.commit()
    assert await result_svc.upsert_class_result(
        db_session, show_entry_id=w.entry.id, user_id=w.organizer.id, is_admin=False,
        grade_id=None, placement=None, critique=None,
    )


async def test_late_arrival_after_absent(client, db_session):
    from app.models.show import ShowStaff
    reg_id, reg_t = await make_api_user(client)
    w = await make_world(db_session)
    db_session.add(ShowStaff(show_id=w.show.id, user_id=reg_id))
    await db_session.commit()
    await show_svc.change_status(db_session, w.show.id, w.organizer.id, False, ShowStatus.in_progress)
    r = await client.post(
        f"/shows/{w.show.id}/entries/{w.entry.id}/checks",
        json={"checks": [{"kind": "arrival", "result": "passed"}]}, headers=auth(reg_t),
    )
    assert r.status_code == 200 and r.json()["attendance_status"] == "arrived"


async def test_entries_api_exposes_attendance_status(client, db_session):
    w = await make_world(db_session)
    r = await client.get(f"/shows/{w.show.id}/entries")
    assert r.json()["items"][0]["attendance_status"] == "registered"
