# tests/integration/checkin_helpers.py
"""
Общие фикстуры-хелперы для тестов чек-ина. Не тест-модуль (нет test_
префикса) — импортируется из test_*.py.

Справочники (тип животного, порода, ранг, класс) создаются здесь же,
а не берутся из сидов: CI-база содержит только миграции, и тесты на
сидах там молча скипаются.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, timedelta

from app.models.dog import Dog, SexEnum
from app.models.reference import AnimalType, Breed, ShowClass, ShowRank
from app.models.show import Show, ShowEntry, ShowStatus
from app.models.user import User

PASSWORD = "secret123"


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def make_api_user(client) -> tuple[uuid.UUID, str]:
    """Регистрирует и логинит пользователя через API: (id, access_token)."""
    email = f"chk_{uuid.uuid4().hex[:10]}@example.com"
    await client.post("/auth/register", json={"email": email, "password": PASSWORD})
    r = await client.post(
        "/auth/login",
        json={"email": email, "password": PASSWORD},
        headers={"X-Token-Delivery": "body"},
    )
    access = r.json()["access_token"]
    me = await client.get("/users/me", headers=auth(access))
    return uuid.UUID(me.json()["id"]), access


async def make_db_user(db_session, *, phone: str | None = None) -> User:
    u = User(
        email=None if phone else f"u_{uuid.uuid4().hex[:8]}@example.com",
        phone=phone,
        hashed_password="x",
    )
    db_session.add(u)
    await db_session.flush()
    return u


@dataclass
class World:
    organizer: User
    owner: User
    show: Show
    dog: Dog
    entry: ShowEntry
    show_class: ShowClass
    breed: Breed


async def make_references(db_session, *, class_code: str = "open"):
    suffix = uuid.uuid4().hex[:6]
    at = AnimalType(code=f"dog_{suffix}", name="Собаки")
    db_session.add(at)
    await db_session.flush()
    breed = Breed(animal_type_id=at.id, code=f"breed_{suffix}", name="Лабрадор")
    rank = ShowRank(code=f"rank_{suffix}", name="КЧК")
    cls = ShowClass(
        animal_type_id=at.id, code=class_code, name="Открытый",
        age_from_months=15, age_to_months=None,
    )
    db_session.add_all([breed, rank, cls])
    await db_session.flush()
    return breed, rank, cls


async def make_world(
    db_session,
    *,
    status: ShowStatus = ShowStatus.registration_closed,
    checkin_enabled: bool = True,
    date_start: date | None = None,
    class_code: str = "open",
    organizer: User | None = None,
    owner: User | None = None,
) -> World:
    breed, rank, cls = await make_references(db_session, class_code=class_code)
    organizer = organizer or await make_db_user(db_session)
    owner = owner or await make_db_user(db_session)
    show = Show(
        organizer_id=organizer.id, rank_id=rank.id, name="Чек-ин выставка",
        date_start=date_start or date.today(), status=status,
        checkin_enabled=checkin_enabled,
    )
    dog = Dog(
        breed_id=breed.id, name=f"Рекс {uuid.uuid4().hex[:4]}",
        sex=SexEnum.male, date_of_birth=date.today() - timedelta(days=900),
        owner_id=owner.id, microchip=f"6430{uuid.uuid4().int % 10**11:011d}",
    )
    db_session.add_all([show, dog])
    await db_session.flush()
    entry = ShowEntry(
        show_id=show.id, dog_id=dog.id, show_class_id=cls.id,
        registered_by=owner.id, catalog_number=1,
    )
    db_session.add(entry)
    await db_session.commit()
    return World(organizer, owner, show, dog, entry, cls, breed)


async def add_entry(db_session, world: World, *, owner: User, name: str = "Белка") -> ShowEntry:
    dog = Dog(
        breed_id=world.breed.id, name=name, sex=SexEnum.female,
        date_of_birth=date.today() - timedelta(days=800), owner_id=owner.id,
    )
    db_session.add(dog)
    await db_session.flush()
    entry = ShowEntry(
        show_id=world.show.id, dog_id=dog.id, show_class_id=world.show_class.id,
        registered_by=owner.id,
    )
    db_session.add(entry)
    await db_session.commit()
    return entry
