"""
Контакты питомника и объявления.

1. Согласие на распространение (ст. 10.1 152-ФЗ): без contacts_public
   контакты не отдаются посторонним вообще.
2. Защита от сборщиков (план защиты 2026-10-05, этап 4): даже открытые
   контакты не лежат в списках и карточках — только признак
   has_public_contacts. Сами контакты — отдельным запросом по кнопке
   «Показать контакты» (GET /…/{id}/contacts) с лимитом на IP.

Владелец и админ видят контакты в карточке всегда: иначе форма
редактирования получила бы пустые поля и затёрла их при сохранении.
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


async def _create_classified(client, token: str, **extra) -> str:
    r = await client.post(
        "/classifieds",
        json={
            "category": "other",
            "title": "Услуги груминга",
            "description": "Стрижка и тримминг собак любых пород",
            "price_kind": "negotiable",
            "contact_phone": PHONE,
            "contact_email": EMAIL,
            **extra,
        },
        headers=auth(token),
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _journal(db_session, target_id: str) -> list[UserConsent]:
    rows = await db_session.execute(
        select(UserConsent).where(UserConsent.target_id == uuid.UUID(target_id))
    )
    return list(rows.scalars().all())


# --- Без согласия ---------------------------------------------------------


async def test_kennel_contacts_hidden_without_consent(client):
    _, owner = await make_api_user(client)
    kennel = await _create_kennel(client, owner)
    assert kennel["contacts_public"] is False

    r = await client.get(f"/kennels/{kennel['id']}")
    assert r.status_code == 200
    body = r.json()
    assert body["contact_phone"] is None
    assert body["contact_email"] is None
    assert body["website"] is None
    assert body["has_public_contacts"] is False

    # «Показать контакты» без согласия — 404, как будто контактов нет.
    r = await client.get(f"/kennels/{kennel['id']}/contacts")
    assert r.status_code == 404

    # Владелец видит свои контакты в карточке.
    r = await client.get(f"/kennels/{kennel['id']}", headers=auth(owner))
    assert r.json()["contact_phone"] == PHONE


# --- С согласием: только по кнопке ----------------------------------------


async def test_public_kennel_contacts_only_on_reveal(client):
    _, owner = await make_api_user(client)
    kennel = await _create_kennel(client, owner, contacts_public=True)

    # В карточке и в списке — только признак, без самих контактов.
    r = await client.get(f"/kennels/{kennel['id']}")
    assert r.json()["contact_phone"] is None
    assert r.json()["has_public_contacts"] is True
    listing = await client.get("/kennels", params={"search": kennel["name"]})
    item = next(i for i in listing.json()["items"] if i["id"] == kennel["id"])
    assert item["contact_email"] is None
    assert item["has_public_contacts"] is True

    r = await client.get(f"/kennels/{kennel['id']}/contacts")
    assert r.status_code == 200, r.text
    assert r.json() == {
        "contact_phone": PHONE,
        "contact_email": EMAIL,
        "website": "https://example.com",
    }


async def test_contacts_reveal_is_rate_limited(client):
    _, owner = await make_api_user(client)
    kennel = await _create_kennel(client, owner, contacts_public=True)

    statuses = [
        (await client.get(f"/kennels/{kennel['id']}/contacts")).status_code
        for _ in range(31)
    ]
    assert statuses[:30] == [200] * 30
    assert statuses[30] == 429


async def test_kennel_toggle_writes_consent_journal(client, db_session):
    _, owner = await make_api_user(client)
    kennel = await _create_kennel(client, owner, contacts_public=True)

    rows = await _journal(db_session, kennel["id"])
    assert [r.kind for r in rows] == [ConsentKind.public_kennel_contacts.value]
    assert rows[0].revoked_at is None

    r = await client.put(
        f"/kennels/{kennel['id']}",
        json={"contacts_public": False},
        headers=auth(owner),
    )
    assert r.status_code == 200, r.text
    r = await client.get(f"/kennels/{kennel['id']}/contacts")
    assert r.status_code == 404

    rows = await _journal(db_session, kennel["id"])
    assert all(row.revoked_at is not None for row in rows)


async def test_classified_contacts(client, db_session):
    _, author = await make_api_user(client)
    cid = await _create_classified(client, author)

    r = await client.get(f"/classifieds/{cid}")
    assert r.json()["contact_phone"] is None
    assert r.json()["has_public_contacts"] is False
    assert (await client.get(f"/classifieds/{cid}/contacts")).status_code == 404

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
    assert r.json()["contact_phone"] is None
    assert r.json()["has_public_contacts"] is True

    r = await client.get(f"/classifieds/{cid}/contacts")
    assert r.status_code == 200, r.text
    assert r.json() == {"contact_phone": PHONE, "contact_email": EMAIL}

    rows = await _journal(db_session, cid)
    assert [row.kind for row in rows] == [
        ConsentKind.public_classified_contacts.value
    ]
