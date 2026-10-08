"""
Интеграция: единая политика прав на собаку (ревью 2026-10-06, BE-33).

Правило «владелец собаки, владелец её питомника или admin» было описано
дважды (services/dog.py и services/show.py) и уже расходилось однажды
(review 2026-06-10). Теперь — одна функция app/policies.can_manage_dog.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.models.dog import Dog, SexEnum
from app.models.kennel import Kennel
from app.models.reference import Breed
from app.models.user import User
from app.policies import can_manage_dog


async def _user(db_session) -> User:
    u = User(email=f"pol_{uuid.uuid4().hex[:8]}@example.com", hashed_password="x")
    db_session.add(u)
    await db_session.commit()
    return u


async def test_can_manage_dog(db_session):
    breed = (await db_session.execute(select(Breed).limit(1))).scalars().first()
    if breed is None:
        pytest.skip("нет пород в сидах")
    owner = await _user(db_session)
    breeder = await _user(db_session)
    stranger = await _user(db_session)
    kennel = Kennel(name=f"K {uuid.uuid4().hex[:6]}", owner_id=breeder.id)
    db_session.add(kennel)
    await db_session.commit()
    dog = Dog(
        breed_id=breed.id, name="Рекс", sex=SexEnum.male, owner_id=owner.id,
        kennel_id=kennel.id, date_of_birth=date.today() - timedelta(days=400),
    )
    db_session.add(dog)
    await db_session.commit()

    assert await can_manage_dog(db_session, dog, owner.id, is_admin=False)
    assert await can_manage_dog(db_session, dog, breeder.id, is_admin=False)
    assert await can_manage_dog(db_session, dog, stranger.id, is_admin=True)
    assert not await can_manage_dog(db_session, dog, stranger.id, is_admin=False)
