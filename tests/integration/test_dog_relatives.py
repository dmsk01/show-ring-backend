"""
Интеграция: потомки и сибсы собаки.

Родство не хранится отдельно — выводится из father_id/mother_id:
- GET  /dogs/{id}/descendants            — прямые потомки + второй родитель;
- GET  /dogs/{id}/siblings               — full (оба родителя общие) / half;
- POST /dogs/{id}/descendants {child_id} — проставить собаку родителем потомка;
- DELETE /dogs/{id}/descendants/{child}  — очистить этот слот у потомка.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import select

from app.models.dog import Dog, SexEnum
from app.models.reference import Breed

PASSWORD = "secret123"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _make_user(client) -> tuple[uuid.UUID, str]:
    """Регистрирует и логинит пользователя, возвращает (id, access_token)."""
    email = f"itest_{uuid.uuid4().hex[:10]}@example.com"
    await client.post("/auth/register", json={"email": email, "password": PASSWORD})
    r = await client.post(
        "/auth/login",
        json={"email": email, "password": PASSWORD},
        headers={"X-Token-Delivery": "body"},
    )
    access = r.json()["access_token"]
    me = await client.get("/users/me", headers=_auth(access))
    return uuid.UUID(me.json()["id"]), access


async def _dog(
    db_session,
    name: str,
    sex: SexEnum,
    *,
    owner_id: uuid.UUID | None = None,
    father: Dog | None = None,
    mother: Dog | None = None,
    born: date | None = None,
) -> Dog:
    breed = (await db_session.execute(select(Breed).limit(1))).scalars().first()
    if breed is None:
        pytest.skip("нет пород (сиды) — пропускаем")
    d = Dog(
        breed_id=breed.id,
        name=name,
        sex=sex,
        owner_id=owner_id,
        father_id=father.id if father else None,
        mother_id=mother.id if mother else None,
        date_of_birth=born,
    )
    db_session.add(d)
    await db_session.commit()
    return d


async def test_descendants_list_children_with_other_parent(client, db_session):
    sire = await _dog(db_session, "Отец", SexEnum.male)
    dam = await _dog(db_session, "Мать", SexEnum.female)
    pup1 = await _dog(db_session, "Щенок 1", SexEnum.male, father=sire, mother=dam,
                      born=date(2024, 1, 1))
    pup2 = await _dog(db_session, "Щенок 2", SexEnum.female, father=sire,
                      born=date(2025, 1, 1))
    await _dog(db_session, "Чужой", SexEnum.male)

    r = await client.get(f"/dogs/{sire.id}/descendants")
    assert r.status_code == 200
    items = r.json()
    # Новые первыми (дата рождения по убыванию).
    assert [i["id"] for i in items] == [str(pup2.id), str(pup1.id)]
    assert items[0]["other_parent"] is None
    assert items[1]["other_parent"] == {
        "id": str(dam.id), "name": "Мать", "avatar_file_id": None,
    }

    r = await client.get(f"/dogs/{dam.id}/descendants")
    assert [i["id"] for i in r.json()] == [str(pup1.id)]


async def test_siblings_full_and_half(client, db_session):
    sire = await _dog(db_session, "Отец", SexEnum.male)
    dam = await _dog(db_session, "Мать", SexEnum.female)
    other_dam = await _dog(db_session, "Другая мать", SexEnum.female)
    me = await _dog(db_session, "Я", SexEnum.male, father=sire, mother=dam)
    full = await _dog(db_session, "Полнородный", SexEnum.female, father=sire, mother=dam)
    half = await _dog(db_session, "Полукровный", SexEnum.male, father=sire, mother=other_dam)
    # Оба родителя неизвестны у обоих — не сибсы (NULL ≠ NULL).
    orphan = await _dog(db_session, "Сирота", SexEnum.male)
    await _dog(db_session, "Сирота 2", SexEnum.male)

    r = await client.get(f"/dogs/{me.id}/siblings")
    assert r.status_code == 200
    got = {i["id"]: (i["kind"], i["shared_parent"]) for i in r.json()}
    assert got == {
        str(full.id): ("full", "both"),
        str(half.id): ("half", "father"),
    }
    # Полнородные первыми.
    assert r.json()[0]["id"] == str(full.id)

    assert (await client.get(f"/dogs/{orphan.id}/siblings")).json() == []


async def test_relatives_of_missing_dog_is_404(client):
    missing = uuid.uuid4()
    assert (await client.get(f"/dogs/{missing}/descendants")).status_code == 404
    assert (await client.get(f"/dogs/{missing}/siblings")).status_code == 404


async def test_add_and_remove_descendant(client, db_session):
    uid, token = await _make_user(client)
    dam = await _dog(db_session, "Мать", SexEnum.female, owner_id=uid)
    pup = await _dog(db_session, "Щенок", SexEnum.male, owner_id=uid)

    r = await client.post(
        f"/dogs/{dam.id}/descendants", json={"child_id": str(pup.id)},
        headers=_auth(token),
    )
    assert r.status_code == 201, r.text
    assert r.json()["id"] == str(pup.id)
    await db_session.refresh(pup)
    # Сука встаёт в слот матери.
    assert pup.mother_id == dam.id and pup.father_id is None

    # Повторная привязка идемпотентна.
    r = await client.post(
        f"/dogs/{dam.id}/descendants", json={"child_id": str(pup.id)},
        headers=_auth(token),
    )
    assert r.status_code == 201

    r = await client.delete(f"/dogs/{dam.id}/descendants/{pup.id}", headers=_auth(token))
    assert r.status_code == 204
    await db_session.refresh(pup)
    assert pup.mother_id is None

    # Уже не потомок — 404.
    r = await client.delete(f"/dogs/{dam.id}/descendants/{pup.id}", headers=_auth(token))
    assert r.status_code == 404


async def test_add_descendant_does_not_overwrite_other_parent(client, db_session):
    uid, token = await _make_user(client)
    sire = await _dog(db_session, "Отец", SexEnum.male, owner_id=uid)
    other_sire = await _dog(db_session, "Другой отец", SexEnum.male, owner_id=uid)
    pup = await _dog(db_session, "Щенок", SexEnum.male, owner_id=uid, father=other_sire)

    r = await client.post(
        f"/dogs/{sire.id}/descendants", json={"child_id": str(pup.id)},
        headers=_auth(token),
    )
    assert r.status_code == 409
    assert r.json()["detail"] == "parent_already_set"
    await db_session.refresh(pup)
    assert pup.father_id == other_sire.id


async def test_add_descendant_rejects_cycles(client, db_session):
    uid, token = await _make_user(client)
    grandsire = await _dog(db_session, "Дед", SexEnum.male, owner_id=uid)
    sire = await _dog(db_session, "Отец", SexEnum.male, owner_id=uid, father=grandsire)
    dam = await _dog(db_session, "Мать", SexEnum.female, owner_id=uid)
    pup = await _dog(db_session, "Щенок", SexEnum.female, owner_id=uid,
                     father=sire, mother=dam)

    # Дед не может стать потомком собственного сына.
    r = await client.post(
        f"/dogs/{sire.id}/descendants", json={"child_id": str(grandsire.id)},
        headers=_auth(token),
    )
    assert r.status_code == 422
    assert r.json()["detail"] == "pedigree_cycle"

    # Предок через два поколения: дед не может стать потомком внучки.
    r = await client.post(
        f"/dogs/{pup.id}/descendants", json={"child_id": str(grandsire.id)},
        headers=_auth(token),
    )
    assert r.status_code == 422

    # Сам себе потомок.
    r = await client.post(
        f"/dogs/{dam.id}/descendants", json={"child_id": str(dam.id)},
        headers=_auth(token),
    )
    assert r.status_code == 422
    assert r.json()["detail"] == "self_parent_forbidden"


async def test_add_descendant_requires_right_on_child(client, db_session):
    owner_id, _ = await _make_user(client)
    _, stranger_token = await _make_user(client)
    sire = await _dog(db_session, "Отец", SexEnum.male)
    pup = await _dog(db_session, "Чужой щенок", SexEnum.male, owner_id=owner_id)

    r = await client.post(
        f"/dogs/{sire.id}/descendants", json={"child_id": str(pup.id)},
        headers=_auth(stranger_token),
    )
    assert r.status_code == 403

    r = await client.post(
        f"/dogs/{sire.id}/descendants", json={"child_id": str(pup.id)}
    )
    assert r.status_code == 401


async def test_add_descendant_unknown_dogs_is_404(client, db_session):
    uid, token = await _make_user(client)
    sire = await _dog(db_session, "Отец", SexEnum.male, owner_id=uid)

    r = await client.post(
        f"/dogs/{sire.id}/descendants", json={"child_id": str(uuid.uuid4())},
        headers=_auth(token),
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "child_not_found"

    r = await client.post(
        f"/dogs/{uuid.uuid4()}/descendants", json={"child_id": str(sire.id)},
        headers=_auth(token),
    )
    assert r.status_code == 404


async def test_update_rejects_descendant_as_parent(client, db_session):
    # Тот же цикл, но через PUT /dogs/{id} (форма собаки): внука нельзя
    # сделать отцом деда — ни напрямую, ни через поколение.
    uid, token = await _make_user(client)
    grandsire = await _dog(db_session, "Дед", SexEnum.male, owner_id=uid)
    sire = await _dog(db_session, "Отец", SexEnum.male, owner_id=uid, father=grandsire)
    grandson = await _dog(db_session, "Внук", SexEnum.male, owner_id=uid, father=sire)

    for parent in (sire, grandson):
        r = await client.put(
            f"/dogs/{grandsire.id}", json={"father_id": str(parent.id)},
            headers=_auth(token),
        )
        assert r.status_code == 422, r.text
        assert r.json()["detail"] == "pedigree_cycle"
    await db_session.refresh(grandsire)
    assert grandsire.father_id is None


async def test_update_keeps_valid_parents(client, db_session):
    # Повторное сохранение формы с теми же (корректными) родителями не
    # должно ловить ложный цикл; смена на постороннего кобеля — тоже.
    uid, token = await _make_user(client)
    sire = await _dog(db_session, "Отец", SexEnum.male, owner_id=uid)
    other = await _dog(db_session, "Другой", SexEnum.male, owner_id=uid)
    pup = await _dog(db_session, "Щенок", SexEnum.male, owner_id=uid, father=sire)

    r = await client.put(
        f"/dogs/{pup.id}", json={"father_id": str(sire.id), "name": "Щенок 2"},
        headers=_auth(token),
    )
    assert r.status_code == 200, r.text
    r = await client.put(
        f"/dogs/{pup.id}", json={"father_id": str(other.id)}, headers=_auth(token),
    )
    assert r.status_code == 200, r.text
