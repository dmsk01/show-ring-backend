"""Интеграция: раздел «Мои выставки» (агрегат, обогащение, PATCH записи)."""
from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import select

from app.models.dog import Dog, SexEnum
from app.models.reference import Breed, ShowClass, ShowRank
from app.models.show import Show, ShowEntry, ShowStatus
from app.models.user import User
from app.repositories import show as repo


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
    db_session.add(ShowEntry(show_id=show.id, dog_id=dog.id,
                             show_class_id=cls.id, registered_by=user.id))
    await db_session.commit()

    rows = await repo.list_user_entries_for_show_enriched(db_session, show.id, user.id)
    assert len(rows) == 1
    entry, dog_name, class_code, class_name = rows[0]
    assert dog_name == "Рекс Тест"
    assert class_name == "Открытый"
    assert class_code == cls.code
