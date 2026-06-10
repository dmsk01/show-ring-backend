"""
Интеграция: правки по review 2026-06-10 — права на собаку.

После ввода Dog.owner_id права на update/delete/фото остались на старой
модели «владелец питомника или admin»: владелец собаки БЕЗ питомника не
мог редактировать собственную карточку (403). Теперь право единое:
прямой владелец (owner_id) ИЛИ владелец питомника ИЛИ admin.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.models.dog import Dog, SexEnum
from app.models.reference import Breed
from app.models.user import User
from app.services import dog as svc


async def _breed(db_session) -> Breed:
    b = (await db_session.execute(select(Breed).limit(1))).scalars().first()
    if b is None:
        pytest.skip("нет пород (сиды) — пропускаем")
    return b


async def _user(db_session) -> User:
    u = User(email=f"own_{uuid.uuid4().hex[:8]}@example.com", hashed_password="x")
    db_session.add(u)
    await db_session.commit()
    return u


async def _dog_without_kennel(db_session, owner: User) -> Dog:
    d = Dog(
        breed_id=(await _breed(db_session)).id,
        name="Свой Пёс",
        sex=SexEnum.male,
        date_of_birth=date.today() - timedelta(days=700),
        owner_id=owner.id,
    )
    db_session.add(d)
    await db_session.commit()
    return d


async def test_owner_without_kennel_can_update_own_dog(db_session):
    owner = await _user(db_session)
    dog = await _dog_without_kennel(db_session, owner)

    updated = await svc.update_dog(
        db_session, dog.id, requester_id=owner.id, is_admin=False,
        fields={"name": "Исправленная Кличка"},
    )
    assert updated.name == "Исправленная Кличка"


async def test_owner_without_kennel_can_delete_own_dog(db_session):
    owner = await _user(db_session)
    dog = await _dog_without_kennel(db_session, owner)

    await svc.delete_dog(
        db_session, dog.id, requester_id=owner.id, is_admin=False
    )
    from app.repositories import dog as repo
    assert await repo.get_dog(db_session, dog.id) is None


async def test_stranger_still_forbidden(db_session):
    owner = await _user(db_session)
    stranger = await _user(db_session)
    dog = await _dog_without_kennel(db_session, owner)

    with pytest.raises(ValueError, match="forbidden"):
        await svc.update_dog(
            db_session, dog.id, requester_id=stranger.id, is_admin=False,
            fields={"name": "Чужая Правка"},
        )
    with pytest.raises(ValueError, match="forbidden"):
        await svc.delete_dog(
            db_session, dog.id, requester_id=stranger.id, is_admin=False
        )
