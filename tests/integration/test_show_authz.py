"""
Интеграция: правки по review 2026-06-10 — выставки.

- POST /shows закрыт ролью organizer|admin (докстринг сервиса обещал
  это с этапа 6, но проверки не было: любой свежий аккаунт мог
  создавать выставки).
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import select

from app.models.reference import ShowRank
from app.models.user import RoleEnum, UserRole

PASSWORD = "secret123"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _make_user(client) -> tuple[uuid.UUID, str]:
    """Регистрирует и логинит пользователя, возвращает (id, access_token)."""
    email = f"authz_{uuid.uuid4().hex[:10]}@example.com"
    await client.post(
        "/auth/register", json={"email": email, "password": PASSWORD}
    )
    r = await client.post(
        "/auth/login", json={"email": email, "password": PASSWORD}
    )
    access = r.json()["access_token"]
    me = await client.get("/users/me", headers=_auth(access))
    return uuid.UUID(me.json()["id"]), access


async def _rank_id(db_session) -> uuid.UUID:
    rank = (await db_session.execute(select(ShowRank).limit(1))).scalars().first()
    if rank is None:
        pytest.skip("нет рангов выставок (сиды) — пропускаем")
    return rank.id


def _show_payload(rank_id: uuid.UUID) -> dict:
    return {
        "name": "Тестовая выставка",
        "rank_id": str(rank_id),
        "date_start": date.today().isoformat(),
    }


async def test_create_show_plain_user_forbidden(client, db_session):
    """Обычный пользователь (без роли organizer/admin) получает 403."""
    _, token = await _make_user(client)
    rank_id = await _rank_id(db_session)

    r = await client.post(
        "/shows", json=_show_payload(rank_id), headers=_auth(token)
    )
    assert r.status_code == 403


async def test_create_show_organizer_allowed(client, db_session):
    """Организатор создаёт выставку как раньше (201)."""
    uid, token = await _make_user(client)
    db_session.add(UserRole(user_id=uid, role=RoleEnum.organizer))
    await db_session.commit()
    rank_id = await _rank_id(db_session)

    r = await client.post(
        "/shows", json=_show_payload(rank_id), headers=_auth(token)
    )
    assert r.status_code == 201
    assert r.json()["organizer_id"] == str(uid)
