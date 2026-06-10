"""Интеграция: раздел «Мои выставки» (агрегат, обогащение, PATCH записи)."""
from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.models.dog import Dog, SexEnum
from app.models.reference import Breed, ShowClass, ShowRank
from app.models.show import Show, ShowEntry, ShowStatus
from app.models.user import User
from app.repositories import show as repo
from app.services import show as svc


async def _breed(db_session):
    b = (await db_session.execute(select(Breed).limit(1))).scalars().first()
    if b is None:
        pytest.skip("нет пород (сиды) — пропускаем")
    return b


async def _rank(db_session):
    r = (await db_session.execute(select(ShowRank).limit(1))).scalars().first()
    if r is None:
        pytest.skip("нет рангов (сиды) — пропускаем")
    return r


async def _user(db_session) -> User:
    u = User(email=f"my_{uuid.uuid4().hex[:8]}@example.com", hashed_password="x")
    db_session.add(u)
    await db_session.commit()
    return u


async def _show_class(db_session, animal_type_id) -> ShowClass:
    c = ShowClass(
        animal_type_id=animal_type_id, code=f"OPEN{uuid.uuid4().hex[:4]}",
        name="Открытый", age_from_months=15, age_to_months=None,
    )
    db_session.add(c)
    await db_session.commit()
    return c


async def test_list_user_entries_enriched_returns_names(db_session):
    breed = await _breed(db_session)
    rank = await _rank(db_session)
    user = await _user(db_session)
    cls = await _show_class(db_session, breed.animal_type_id)
    dog = Dog(breed_id=breed.id, name="Рекс Тест", sex=SexEnum.male)
    db_session.add(dog)
    show = Show(organizer_id=user.id, name="Выставка А", rank_id=rank.id,
                date_start=date.today(), status=ShowStatus.registration_open)
    db_session.add(show)
    await db_session.commit()
    entry = ShowEntry(show_id=show.id, dog_id=dog.id,
                      show_class_id=cls.id, registered_by=user.id)
    db_session.add(entry)
    await db_session.commit()

    rows = await repo.list_user_entries_for_show_enriched(db_session, show.id, user.id)
    assert len(rows) == 1
    entry_row, dog_name, class_code, class_name = rows[0]
    assert dog_name == "Рекс Тест"
    assert class_name == "Открытый"
    assert class_code == cls.code

    single = await repo.get_entry_enriched(db_session, entry.id)
    assert single is not None
    _, single_dog_name, _, _ = single
    assert single_dog_name == "Рекс Тест"


async def test_list_my_shows_groups_and_counts(db_session):
    breed = await _breed(db_session)
    rank = await _rank(db_session)
    user = await _user(db_session)
    cls = await _show_class(db_session, breed.animal_type_id)

    def mk_dog(n):
        d = Dog(breed_id=breed.id, name=n, sex=SexEnum.male)
        db_session.add(d)
        return d

    d1, d2, d3 = mk_dog("D1"), mk_dog("D2"), mk_dog("D3")
    await db_session.commit()

    active = Show(organizer_id=user.id, name="Активная", rank_id=rank.id,
                  date_start=date.today(), status=ShowStatus.registration_open)
    past = Show(organizer_id=user.id, name="Прошедшая", rank_id=rank.id,
                date_start=date.today() - timedelta(days=30),
                status=ShowStatus.completed)
    db_session.add_all([active, past])
    await db_session.commit()

    db_session.add_all([
        ShowEntry(show_id=active.id, dog_id=d1.id, show_class_id=cls.id, registered_by=user.id),
        ShowEntry(show_id=active.id, dog_id=d2.id, show_class_id=cls.id, registered_by=user.id),
        ShowEntry(show_id=past.id, dog_id=d3.id, show_class_id=cls.id, registered_by=user.id),
    ])
    await db_session.commit()

    rows = await repo.list_my_shows(db_session, user.id, "active", page=1, per_page=12)
    total = await repo.count_my_shows(db_session, user.id, "active")
    assert total == 1
    assert len(rows) == 1
    show_obj, count = rows[0]
    assert show_obj.id == active.id
    assert count == 2

    past_total = await repo.count_my_shows(db_session, user.id, "past")
    assert past_total == 1
    all_total = await repo.count_my_shows(db_session, user.id, "all")
    assert all_total == 2
    all_rows = await repo.list_my_shows(db_session, user.id, "all", page=1, per_page=12)
    assert len(all_rows) == 2


async def test_update_entry_changes_notes_and_keeps_catalog(db_session):
    breed = await _breed(db_session)
    rank = await _rank(db_session)
    user = await _user(db_session)
    cls = await _show_class(db_session, breed.animal_type_id)
    dog = Dog(breed_id=breed.id, name="Барс", sex=SexEnum.male,
              date_of_birth=date.today() - timedelta(days=600))
    db_session.add(dog)
    show = Show(organizer_id=user.id, name="Активная2", rank_id=rank.id,
                date_start=date.today(), status=ShowStatus.registration_open)
    db_session.add(show)
    await db_session.commit()
    entry = ShowEntry(show_id=show.id, dog_id=dog.id, show_class_id=cls.id,
                      registered_by=user.id, catalog_number=5, notes="old")
    db_session.add(entry)
    await db_session.commit()

    updated = await svc.update_entry(
        db_session, show_id=show.id, entry_id=entry.id,
        requester_id=user.id, is_admin=False,
        show_class_id=None, handler_id=None, notes="new note",
        today=date.today(),
    )
    assert updated.notes == "new note"
    assert updated.catalog_number == 5  # сохранён


async def test_update_entry_forbidden_for_other_user(db_session):
    breed = await _breed(db_session)
    rank = await _rank(db_session)
    owner = await _user(db_session)
    other = await _user(db_session)
    cls = await _show_class(db_session, breed.animal_type_id)
    dog = Dog(breed_id=breed.id, name="Чужой", sex=SexEnum.male)
    db_session.add(dog)
    show = Show(organizer_id=owner.id, name="Активная3", rank_id=rank.id,
                date_start=date.today(), status=ShowStatus.registration_open)
    db_session.add(show)
    await db_session.commit()
    entry = ShowEntry(show_id=show.id, dog_id=dog.id, show_class_id=cls.id,
                      registered_by=owner.id)
    db_session.add(entry)
    await db_session.commit()

    with pytest.raises(ValueError, match="forbidden"):
        await svc.update_entry(
            db_session, show_id=show.id, entry_id=entry.id,
            requester_id=other.id, is_admin=False,
            show_class_id=None, handler_id=None, notes="x",
            today=date.today(),
        )


async def test_update_entry_locked_when_registration_closed(db_session):
    breed = await _breed(db_session)
    rank = await _rank(db_session)
    user = await _user(db_session)
    cls = await _show_class(db_session, breed.animal_type_id)
    dog = Dog(breed_id=breed.id, name="Поздно", sex=SexEnum.male)
    db_session.add(dog)
    show = Show(organizer_id=user.id, name="Закрыта", rank_id=rank.id,
                date_start=date.today(), status=ShowStatus.registration_closed)
    db_session.add(show)
    await db_session.commit()
    entry = ShowEntry(show_id=show.id, dog_id=dog.id, show_class_id=cls.id,
                      registered_by=user.id)
    db_session.add(entry)
    await db_session.commit()

    with pytest.raises(ValueError, match="registration_locked"):
        await svc.update_entry(
            db_session, show_id=show.id, entry_id=entry.id,
            requester_id=user.id, is_admin=False,
            show_class_id=None, handler_id=None, notes="x",
            today=date.today(),
        )
