"""
GET /shows/{id}/entries/table — записи выставки, обогащённые кличкой, породой,
питомником и классом (ревью фронтенда 2026-10-09, FE-08).

Раньше таблица результатов склеивалась на фронте с первой страницей /dogs
(гостю — максимум 50 собак всей базы), и гости видели «—» вместо кличек.
"""

from __future__ import annotations

from app.models.kennel import Kennel
from app.models.show import ShowStatus
from tests.integration.checkin_helpers import add_entry, make_world


async def test_anonymous_gets_enriched_rows(client, db_session):
    world = await make_world(db_session, status=ShowStatus.completed)
    await add_entry(db_session, world, owner=world.owner, name="Белка")

    r = await client.get(f"/shows/{world.show.id}/entries/table")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 2
    row = next(i for i in body["items"] if i["dog_id"] == str(world.dog.id))
    assert row["dog_name"] == world.dog.name
    assert row["breed_id"] == str(world.breed.id)
    assert row["breed_name"] == "Лабрадор"
    assert row["class_name"] == "Открытый"
    assert row["kennel_name"] is None
    assert row["catalog_number"] == 1


async def test_reference_names_follow_accept_language(client, db_session):
    world = await make_world(db_session, status=ShowStatus.completed)
    world.breed.name_en = "Labrador"
    world.show_class.name_en = "Open"
    await db_session.commit()

    r = await client.get(
        f"/shows/{world.show.id}/entries/table", headers={"Accept-Language": "en"}
    )

    assert r.status_code == 200, r.text
    row = r.json()["items"][0]
    assert row["breed_name"] == "Labrador"
    assert row["class_name"] == "Open"


async def test_kennel_name_and_prefix(client, db_session):
    world = await make_world(db_session, status=ShowStatus.completed)
    kennel = Kennel(owner_id=world.owner.id, name="Питомник Тест", kennel_prefix="из Теста")
    db_session.add(kennel)
    await db_session.flush()
    world.dog.kennel_id = kennel.id
    await db_session.commit()

    r = await client.get(f"/shows/{world.show.id}/entries/table")

    row = r.json()["items"][0]
    assert row["kennel_name"] == "Питомник Тест"
    assert row["kennel_prefix"] == "из Теста"


async def test_draft_show_is_hidden_from_anonymous(client, db_session):
    world = await make_world(db_session, status=ShowStatus.draft)

    r = await client.get(f"/shows/{world.show.id}/entries/table")

    assert r.status_code == 404, r.text
