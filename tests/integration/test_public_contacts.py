"""
Согласие на распространение контактов (ст. 10.1 152-ФЗ).

Контакты питомника и объявления видны посторонним только при
contacts_public=True; владелец и админ видят их всегда (иначе форма
редактирования затёрла бы скрытые контакты). Включение/выключение
переключателя пишется в журнал согласий с target_id публикации.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.models.consent import UserConsent
from app.services.consent import ConsentKind
from tests.integration.checkin_helpers import auth, make_api_user

PHONE = "+79990001122"
EMAIL = "kennel@example.com"


async def _create_kennel(client, token: str, **extra) -> dict:
    r = await client.post(
        "/kennels",
        json={
            "name": f"Kennel {uuid.uuid4().hex[:6]}",
            "contact_phone": PHONE,
            "contact_email": EMAIL,
            "website": "https://example.com",
            **extra,
        },
        headers=auth(token),
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _journal(db_session, target_id: str) -> list[UserConsent]:
    rows = await db_session.execute(
        select(UserConsent).where(UserConsent.target_id == uuid.UUID(target_id))
    )
    return list(rows.scalars().all())


async def test_kennel_contacts_hidden_without_consent(client):
    _, owner = await make_api_user(client)
    kennel = await _create_kennel(client, owner)
    assert kennel["contacts_public"] is False

    # Аноним и посторонний — без контактов.
    r = await client.get(f"/kennels/{kennel['id']}")
    assert r.status_code == 200
    assert r.json()["contact_phone"] is None
    assert r.json()["contact_email"] is None
    assert r.json()["website"] is None

    _, stranger = await make_api_user(client)
    r = await client.get(f"/kennels/{kennel['id']}", headers=auth(stranger))
    assert r.json()["contact_phone"] is None

    listing = await client.get("/kennels", params={"search": kennel["name"]})
    item = next(i for i in listing.json()["items"] if i["id"] == kennel["id"])
    assert item["contact_email"] is None

    # Владелец видит свои контакты.
    r = await client.get(f"/kennels/{kennel['id']}", headers=auth(owner))
    assert r.json()["contact_phone"] == PHONE


async def test_kennel_toggle_writes_consent_journal(client, db_session):
    _, owner = await make_api_user(client)
    kennel = await _create_kennel(client, owner, contacts_public=True)

    r = await client.get(f"/kennels/{kennel['id']}")
    assert r.json()["contact_phone"] == PHONE

    rows = await _journal(db_session, kennel["id"])
    assert [r.kind for r in rows] == [ConsentKind.public_kennel_contacts.value]
    assert rows[0].revoked_at is None

    r = await client.put(
        f"/kennels/{kennel['id']}",
        json={"contacts_public": False},
        headers=auth(owner),
    )
    assert r.status_code == 200, r.text
    r = await client.get(f"/kennels/{kennel['id']}")
    assert r.json()["contact_phone"] is None

    rows = await _journal(db_session, kennel["id"])
    assert all(row.revoked_at is not None for row in rows)


async def test_classified_contacts_hidden_without_consent(client, db_session):
    _, author = await make_api_user(client)
    r = await client.post(
        "/classifieds",
        json={
            "category": "other",
            "title": "Услуги груминга",
            "description": "Стрижка и тримминг собак любых пород",
            "price_kind": "negotiable",
            "contact_phone": PHONE,
            "contact_email": EMAIL,
        },
        headers=auth(author),
    )
    assert r.status_code == 201, r.text
    cid = r.json()["id"]

    r = await client.get(f"/classifieds/{cid}")
    assert r.json()["contact_phone"] is None
    assert r.json()["contact_email"] is None

    r = await client.put(
        f"/classifieds/{cid}",
        json={"contacts_public": True},
        headers=auth(author),
    )
    assert r.status_code == 200, r.text
    # Переключатель видимости — не правка контента: на повторную
    # модерацию объявление из-за него не уходит.
    assert r.json()["status"] == "moderation"

    r = await client.get(f"/classifieds/{cid}")
    assert r.json()["contact_phone"] == PHONE
    rows = await _journal(db_session, cid)
    assert [row.kind for row in rows] == [
        ConsentKind.public_classified_contacts.value
    ]
