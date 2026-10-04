# tests/integration/test_show_staff.py
"""Интеграция: флаг чек-ина и персонал выставки."""

from __future__ import annotations

from app.models.show import ShowStatus
from app.models.user import User
from tests.integration.checkin_helpers import (
    auth,
    make_api_user,
    make_db_user,
    make_world,
)


async def _world_with_api_organizer(client, db_session, **kw):
    org_id, org_token = await make_api_user(client)
    organizer = await db_session.get(User, org_id)
    w = await make_world(db_session, organizer=organizer, **kw)
    return w, org_token


async def test_toggle_checkin_enabled(client, db_session):
    w, token = await _world_with_api_organizer(client, db_session, checkin_enabled=False)
    r = await client.put(
        f"/shows/{w.show.id}/checkin/settings", json={"enabled": True}, headers=auth(token)
    )
    assert r.status_code == 200 and r.json()["checkin_enabled"] is True
    r = await client.get(f"/shows/{w.show.id}")
    assert r.json()["checkin_enabled"] is True


async def test_toggle_forbidden_for_stranger_and_locked_when_completed(client, db_session):
    w, token = await _world_with_api_organizer(client, db_session)
    _, stranger = await make_api_user(client)
    r = await client.put(
        f"/shows/{w.show.id}/checkin/settings", json={"enabled": False}, headers=auth(stranger)
    )
    assert r.status_code == 403
    w.show.status = ShowStatus.completed
    await db_session.commit()
    r = await client.put(
        f"/shows/{w.show.id}/checkin/settings", json={"enabled": False}, headers=auth(token)
    )
    assert r.status_code == 409


async def test_add_list_remove_staff_by_email_and_phone(client, db_session):
    w, token = await _world_with_api_organizer(client, db_session)
    reg_id, reg_token = await make_api_user(client)
    reg = await db_session.get(User, reg_id)
    phone_user = await make_db_user(db_session, phone="+79990001122")
    await db_session.commit()

    r = await client.post(f"/shows/{w.show.id}/staff", json={"email": reg.email.upper()}, headers=auth(token))
    assert r.status_code == 201, r.text
    r = await client.post(f"/shows/{w.show.id}/staff", json={"phone": "+79990001122"}, headers=auth(token))
    assert r.status_code == 201
    r = await client.post(f"/shows/{w.show.id}/staff", json={"email": reg.email}, headers=auth(token))
    assert r.status_code == 409

    r = await client.get(f"/shows/{w.show.id}/staff", headers=auth(token))
    assert {s["user_id"] for s in r.json()} == {str(reg_id), str(phone_user.id)}

    r = await client.get("/shows/staff/my", headers=auth(reg_token))
    assert [s["id"] for s in r.json()] == [str(w.show.id)]

    r = await client.delete(f"/shows/{w.show.id}/staff/{reg_id}", headers=auth(token))
    assert r.status_code == 204
    r = await client.get("/shows/staff/my", headers=auth(reg_token))
    assert r.json() == []


async def test_staff_validation(client, db_session):
    w, token = await _world_with_api_organizer(client, db_session)
    r = await client.post(f"/shows/{w.show.id}/staff", json={}, headers=auth(token))
    assert r.status_code == 422
    r = await client.post(
        f"/shows/{w.show.id}/staff", json={"email": "nobody_xyz@example.com"}, headers=auth(token)
    )
    assert r.status_code == 404
    _, stranger = await make_api_user(client)
    r = await client.get(f"/shows/{w.show.id}/staff", headers=auth(stranger))
    assert r.status_code == 403
