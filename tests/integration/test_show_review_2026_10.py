"""
Интеграция: правки по ревью 2026-10-06 — выставки.

- BE-14: организатор отменяет чужую запись после закрытия регистрации
  (раньше проверка «автор записи» срабатывала раньше ветки организатора).
- BE-15: черновики выставок не видны анонимам и чужим пользователям.
- BE-23: явный null в PUT /shows/{id} для обязательных полей — 422, а не 500.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.models.dog import Dog, SexEnum
from app.models.reference import Breed, ShowClass, ShowRank
from app.models.show import Show, ShowEntry, ShowStatus
from app.models.user import RoleEnum, User, UserRole
from app.services import show as show_svc

PASSWORD = "secret123"


async def _first(db_session, model):
    obj = (await db_session.execute(select(model).limit(1))).scalars().first()
    if obj is None:
        pytest.skip(f"нет {model.__name__} в сидах — пропускаем")
    return obj


async def _user(db_session) -> User:
    u = User(email=f"sr_{uuid.uuid4().hex[:8]}@example.com", hashed_password="x")
    db_session.add(u)
    await db_session.commit()
    return u


async def _show_with_entry(db_session, status: ShowStatus):
    breed = await _first(db_session, Breed)
    rank = await _first(db_session, ShowRank)
    organizer = await _user(db_session)
    participant = await _user(db_session)
    cls = ShowClass(
        animal_type_id=breed.animal_type_id, code=f"OPEN{uuid.uuid4().hex[:4]}",
        name="Открытый", age_from_months=15, age_to_months=None,
    )
    dog = Dog(
        breed_id=breed.id, name="Рекс", sex=SexEnum.male,
        date_of_birth=date.today() - timedelta(days=900),
        owner_id=participant.id,
    )
    db_session.add_all([cls, dog])
    await db_session.commit()
    show = Show(
        organizer_id=organizer.id, name="Выставка", rank_id=rank.id,
        date_start=date.today() + timedelta(days=10), status=status,
    )
    db_session.add(show)
    await db_session.commit()
    entry = ShowEntry(
        show_id=show.id, dog_id=dog.id, show_class_id=cls.id,
        registered_by=participant.id,
    )
    db_session.add(entry)
    await db_session.commit()
    return show, entry, organizer, participant


# --- BE-14 -------------------------------------------------------------


async def test_organizer_cancels_foreign_entry_after_registration_closed(db_session):
    show, entry, organizer, _ = await _show_with_entry(
        db_session, ShowStatus.registration_closed
    )
    await show_svc.cancel_entry(
        db_session, show_id=show.id, entry_id=entry.id,
        requester_id=organizer.id, is_admin=False,
    )
    assert await db_session.get(ShowEntry, entry.id) is None


async def test_organizer_cancels_foreign_entry_while_registration_open(db_session):
    show, entry, organizer, _ = await _show_with_entry(
        db_session, ShowStatus.registration_open
    )
    await show_svc.cancel_entry(
        db_session, show_id=show.id, entry_id=entry.id,
        requester_id=organizer.id, is_admin=False,
    )
    assert await db_session.get(ShowEntry, entry.id) is None


async def test_participant_cannot_cancel_after_registration_closed(db_session):
    show, entry, _, participant = await _show_with_entry(
        db_session, ShowStatus.registration_closed
    )
    with pytest.raises(ValueError, match="registration_locked"):
        await show_svc.cancel_entry(
            db_session, show_id=show.id, entry_id=entry.id,
            requester_id=participant.id, is_admin=False,
        )


async def test_stranger_cannot_cancel_entry(db_session):
    show, entry, _, _ = await _show_with_entry(
        db_session, ShowStatus.registration_open
    )
    stranger = await _user(db_session)
    with pytest.raises(ValueError, match="forbidden"):
        await show_svc.cancel_entry(
            db_session, show_id=show.id, entry_id=entry.id,
            requester_id=stranger.id, is_admin=False,
        )


# --- BE-15 -------------------------------------------------------------


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _login(client, db_session, *, role: RoleEnum | None = None):
    email = f"sr_{uuid.uuid4().hex[:10]}@example.com"
    await client.post("/auth/register", json={"email": email, "password": PASSWORD, "accept_terms": True, "personal_data_consent": True})
    r = await client.post(
        "/auth/login",
        json={"email": email, "password": PASSWORD},
        headers={"X-Token-Delivery": "body"},
    )
    token = r.json()["access_token"]
    me = await client.get("/users/me", headers=_auth(token))
    uid = uuid.UUID(me.json()["id"])
    if role is not None:
        db_session.add(UserRole(user_id=uid, role=role))
        await db_session.commit()
    return uid, token


async def _draft(db_session, organizer_id) -> Show:
    rank = await _first(db_session, ShowRank)
    show = Show(
        organizer_id=organizer_id, name=f"Черновик {uuid.uuid4().hex[:6]}",
        rank_id=rank.id, date_start=date.today() + timedelta(days=30),
        status=ShowStatus.draft,
    )
    db_session.add(show)
    await db_session.commit()
    return show


def _ids(r) -> set[str]:
    return {item["id"] for item in r.json()["items"]}


async def test_draft_hidden_from_anonymous(client, db_session):
    owner_id, _ = await _login(client, db_session, role=RoleEnum.organizer)
    show = await _draft(db_session, owner_id)

    r = await client.get("/shows", params={"search": show.name})
    assert str(show.id) not in _ids(r)
    r = await client.get("/shows", params={"status": "draft", "search": show.name})
    assert str(show.id) not in _ids(r)
    for path in ("", "/judges", "/rings"):
        r = await client.get(f"/shows/{show.id}{path}")
        assert r.status_code == 404, (path, r.status_code)


async def test_draft_hidden_from_other_user(client, db_session):
    owner_id, _ = await _login(client, db_session, role=RoleEnum.organizer)
    show = await _draft(db_session, owner_id)
    _, other = await _login(client, db_session, role=RoleEnum.organizer)

    r = await client.get("/shows", params={"search": show.name}, headers=_auth(other))
    assert str(show.id) not in _ids(r)
    r = await client.get(f"/shows/{show.id}", headers=_auth(other))
    assert r.status_code == 404


async def test_draft_visible_to_owner_and_admin(client, db_session):
    owner_id, owner = await _login(client, db_session, role=RoleEnum.organizer)
    show = await _draft(db_session, owner_id)
    _, admin = await _login(client, db_session, role=RoleEnum.admin)

    for token in (owner, admin):
        r = await client.get("/shows", params={"search": show.name}, headers=_auth(token))
        assert str(show.id) in _ids(r)
        r = await client.get(f"/shows/{show.id}", headers=_auth(token))
        assert r.status_code == 200


# --- BE-23 -------------------------------------------------------------


async def test_update_show_explicit_null_dates_rejected(client, db_session):
    owner_id, owner = await _login(client, db_session, role=RoleEnum.organizer)
    show = await _draft(db_session, owner_id)
    r = await client.put(
        f"/shows/{show.id}",
        json={"date_start": None, "date_end": date.today().isoformat()},
        headers=_auth(owner),
    )
    assert r.status_code == 422, r.text
    r = await client.put(
        f"/shows/{show.id}", json={"name": None}, headers=_auth(owner)
    )
    assert r.status_code == 422, r.text


async def test_moving_show_date_rejected_when_entry_class_no_longer_fits(db_session):
    # BE-23: класс записи выбирается по возрасту на date_start. Перенос
    # выставки, после которого собака «вырастает» из своего класса, должен
    # отклоняться, а не молча оставлять записи в невалидных классах.
    breed = await _first(db_session, Breed)
    rank = await _first(db_session, ShowRank)
    organizer = await _user(db_session)
    junior = ShowClass(
        animal_type_id=breed.animal_type_id, code=f"JUN{uuid.uuid4().hex[:4]}",
        name="Юниоры", age_from_months=9, age_to_months=18,
    )
    db_session.add(junior)
    show_date = date.today() + timedelta(days=10)
    dog = Dog(
        breed_id=breed.id, name="Юнец", sex=SexEnum.male,
        # 17 месяцев на дату выставки — ещё юниор.
        date_of_birth=show_date - timedelta(days=17 * 31),
    )
    db_session.add(dog)
    await db_session.commit()
    show = Show(
        organizer_id=organizer.id, name="Перенос", rank_id=rank.id,
        date_start=show_date, status=ShowStatus.registration_open,
    )
    db_session.add(show)
    await db_session.commit()
    db_session.add(ShowEntry(
        show_id=show.id, dog_id=dog.id, show_class_id=junior.id,
        registered_by=organizer.id,
    ))
    await db_session.commit()

    with pytest.raises(ValueError, match="entries_class_mismatch"):
        await show_svc.update_show(
            db_session, show_id=show.id, requester_id=organizer.id,
            is_admin=False,
            fields={"date_start": show_date + timedelta(days=120)},
        )
    # Перенос в пределах класса — разрешён.
    updated = await show_svc.update_show(
        db_session, show_id=show.id, requester_id=organizer.id, is_admin=False,
        fields={"date_start": show_date + timedelta(days=5)},
    )
    assert updated.date_start == show_date + timedelta(days=5)
