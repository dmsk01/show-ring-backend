"""
Интеграция: удаление собаки не стирает историю выставок (ревью 2026-10-06, BE-10).

Раньше каскад ON DELETE CASCADE удалял записи, результаты и титулы собаки
— в том числе на завершённых выставках с опубликованным каталогом и
выданными дипломами. Теперь:
- собаку с записями на закрытой/идущей/завершённой выставке (или с
  титулами) удалить нельзя — 409 dog_has_show_history;
- записи на ещё открытые выставки при удалении снимаются явно;
- БД страхует: FK show_entries.dog_id и dog_titles.dog_id — RESTRICT.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.dog import Dog, SexEnum
from app.models.reference import Breed, ShowClass, ShowRank
from app.models.show import Show, ShowEntry, ShowStatus
from app.models.user import User
from app.services import dog as dog_svc


async def _first(db_session, model):
    obj = (await db_session.execute(select(model).limit(1))).scalars().first()
    if obj is None:
        pytest.skip(f"нет {model.__name__} в сидах — пропускаем")
    return obj


async def _dog_with_entry(db_session, status: ShowStatus):
    breed = await _first(db_session, Breed)
    rank = await _first(db_session, ShowRank)
    owner = User(email=f"dd_{uuid.uuid4().hex[:8]}@example.com", hashed_password="x")
    db_session.add(owner)
    cls = ShowClass(
        animal_type_id=breed.animal_type_id, code=f"OPEN{uuid.uuid4().hex[:4]}",
        name="Открытый", age_from_months=15, age_to_months=None,
    )
    db_session.add(cls)
    await db_session.commit()
    dog = Dog(
        breed_id=breed.id, name="Рекс", sex=SexEnum.male,
        date_of_birth=date.today() - timedelta(days=900), owner_id=owner.id,
    )
    db_session.add(dog)
    show = Show(
        organizer_id=owner.id, name="Выставка", rank_id=rank.id,
        date_start=date.today(), status=status,
    )
    db_session.add(show)
    await db_session.commit()
    entry = ShowEntry(
        show_id=show.id, dog_id=dog.id, show_class_id=cls.id,
        registered_by=owner.id,
    )
    db_session.add(entry)
    await db_session.commit()
    return owner, dog, entry


@pytest.mark.parametrize(
    "status",
    [ShowStatus.completed, ShowStatus.in_progress, ShowStatus.registration_closed],
)
async def test_dog_with_show_history_cannot_be_deleted(db_session, status):
    owner, dog, entry = await _dog_with_entry(db_session, status)
    with pytest.raises(ValueError, match="dog_has_show_history"):
        await dog_svc.delete_dog(
            db_session, dog_id=dog.id, requester_id=owner.id, is_admin=False
        )
    assert await db_session.get(ShowEntry, entry.id) is not None


async def test_dog_with_only_open_registrations_is_deleted(db_session):
    owner, dog, entry = await _dog_with_entry(db_session, ShowStatus.registration_open)
    entry_id, dog_id = entry.id, dog.id
    await dog_svc.delete_dog(
        db_session, dog_id=dog_id, requester_id=owner.id, is_admin=False
    )
    db_session.expire_all()
    assert await db_session.get(Dog, dog_id) is None
    assert await db_session.get(ShowEntry, entry_id) is None


async def test_db_restricts_deleting_dog_with_entries(db_session):
    _, dog, _ = await _dog_with_entry(db_session, ShowStatus.completed)
    await db_session.delete(dog)
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()
