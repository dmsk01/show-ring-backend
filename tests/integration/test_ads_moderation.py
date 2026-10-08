"""
Интеграция: модерация рекламных кампаний (ревью 2026-10-06, BE-05).

Раньше любой зарегистрированный аккаунт создавал кампанию и сам ставил ей
status=active: баннер с произвольной ссылкой показывался всем посетителям
без проверки. Теперь:
- создавать кампании могут organizer/admin (у фронта раздел «Реклама»
  открыт именно им);
- впервые активирует кампанию только admin (модерация); после одобрения
  владелец может ставить её на паузу и возобновлять;
- update валидирует даты на merged-значениях и не даёт бюджету опуститься
  ниже уже потраченного; бесплатный показ (cost_per_impression=0) запрещён.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

from app.models.ad import AdCampaign
from app.models.user import RoleEnum, UserRole

PASSWORD = "secret123"
CONSENTS = {"accept_terms": True, "personal_data_consent": True}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _login(client, db_session, role: RoleEnum | None = None):
    email = f"ads_{uuid.uuid4().hex[:10]}@example.com"
    await client.post(
        "/auth/register", json={"email": email, "password": PASSWORD, **CONSENTS}
    )
    r = await client.post(
        "/auth/login",
        json={"email": email, "password": PASSWORD},
        headers={"X-Token-Delivery": "body"},
    )
    token = r.json()["access_token"]
    uid = uuid.UUID((await client.get("/users/me", headers=_auth(token))).json()["id"])
    if role is not None:
        db_session.add(UserRole(user_id=uid, role=role))
        await db_session.commit()
    return uid, token


def _payload(**overrides) -> dict:
    data = {
        "name": "Корма",
        "budget": "100.00",
        "cost_per_impression": "0.01",
        "date_start": date.today().isoformat(),
        "date_end": (date.today() + timedelta(days=30)).isoformat(),
    }
    data.update(overrides)
    return data


async def _create(client, token, **overrides):
    r = await client.post("/ads/campaigns", json=_payload(**overrides), headers=_auth(token))
    return r


async def test_plain_user_cannot_create_campaign(client, db_session):
    _, token = await _login(client, db_session)
    r = await _create(client, token)
    assert r.status_code == 403, r.text


async def test_owner_cannot_self_activate(client, db_session):
    _, token = await _login(client, db_session, RoleEnum.organizer)
    r = await _create(client, token)
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    r = await client.put(
        f"/ads/campaigns/{cid}", json={"status": "active"}, headers=_auth(token)
    )
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == "moderation_required"


async def test_admin_approves_then_owner_can_pause_and_resume(client, db_session):
    _, owner = await _login(client, db_session, RoleEnum.organizer)
    _, admin = await _login(client, db_session, RoleEnum.admin)
    cid = (await _create(client, owner)).json()["id"]

    r = await client.put(
        f"/ads/campaigns/{cid}", json={"status": "active"}, headers=_auth(admin)
    )
    assert r.status_code == 200, r.text
    for status in ("paused", "active"):
        r = await client.put(
            f"/ads/campaigns/{cid}", json={"status": status}, headers=_auth(owner)
        )
        assert r.status_code == 200, (status, r.text)
        assert r.json()["status"] == status


async def test_update_validates_merged_dates(client, db_session):
    _, owner = await _login(client, db_session, RoleEnum.organizer)
    cid = (await _create(client, owner)).json()["id"]
    r = await client.put(
        f"/ads/campaigns/{cid}",
        json={"date_end": (date.today() - timedelta(days=5)).isoformat()},
        headers=_auth(owner),
    )
    assert r.status_code == 422, r.text


async def test_budget_cannot_drop_below_spent(client, db_session):
    _, owner = await _login(client, db_session, RoleEnum.organizer)
    cid = (await _create(client, owner)).json()["id"]
    campaign = await db_session.get(AdCampaign, uuid.UUID(cid))
    campaign.spent = Decimal("50")
    await db_session.commit()
    r = await client.put(
        f"/ads/campaigns/{cid}", json={"budget": "10.00"}, headers=_auth(owner)
    )
    assert r.status_code == 422, r.text


async def test_free_impressions_rejected(client, db_session):
    _, owner = await _login(client, db_session, RoleEnum.organizer)
    r = await _create(client, owner, cost_per_impression="0")
    assert r.status_code == 422, r.text
