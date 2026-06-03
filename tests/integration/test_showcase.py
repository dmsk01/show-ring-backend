"""
Интеграция: доработки публичной витрины (этап 18).

Фото собак, сортировка (whitelist), is_verified+счётчики питомника,
помёт→щенки, родители помёта объектами. Данные вставляем напрямую через
db_session (как в остальных интеграционных тестах); породу берём из
сидов dev-БД (skip, если нет).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.models.dog import Dog, DogPhoto, SexEnum
from app.models.file import UploadedFile
from app.models.kennel import Kennel
from app.models.litter import Litter
from app.models.reference import Breed
from app.models.user import User

PASSWORD = "secret123"


async def _breed_id(db_session) -> uuid.UUID:
    bid = (await db_session.execute(select(Breed.id).limit(1))).scalar_one_or_none()
    if bid is None:
        pytest.skip("в БД нет пород (сиды) — пропускаем")
    return bid


async def _owner(db_session) -> uuid.UUID:
    u = User(email=f"itest_{uuid.uuid4().hex[:8]}@example.com", hashed_password="x")
    db_session.add(u)
    await db_session.commit()
    return u.id


async def _file(db_session) -> uuid.UUID:
    f = UploadedFile(
        uploaded_by=None, s3_key=f"general/{uuid.uuid4()}.jpg",
        original_filename="p.jpg", content_type="image/jpeg",
        size_bytes=10, is_public=True,
    )
    db_session.add(f)
    await db_session.commit()
    return f.id


async def test_dog_sort_validation(client):
    # Неизвестное поле сортировки → 422 (whitelist через Literal).
    r = await client.get("/dogs?sort_by=hack")
    assert r.status_code == 422
    r = await client.get("/dogs?sort_by=name&order=asc")
    assert r.status_code == 200


async def test_dog_photos_in_response(client, db_session):
    breed_id = await _breed_id(db_session)
    dog = Dog(breed_id=breed_id, name="Витрина Рекс", sex=SexEnum.male)
    db_session.add(dog)
    await db_session.commit()
    file_id = await _file(db_session)
    db_session.add(DogPhoto(dog_id=dog.id, file_id=file_id, position=0, is_primary=True))
    await db_session.commit()

    r = await client.get(f"/dogs/{dog.id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["photo_file_ids"] == [str(file_id)]
    assert body["avatar_file_id"] == str(file_id)


async def test_kennel_is_verified_and_counts(client, db_session):
    breed_id = await _breed_id(db_session)
    owner_id = await _owner(db_session)
    kennel = Kennel(owner_id=owner_id, name="Витрина Кеннел", is_verified=True)
    db_session.add(kennel)
    await db_session.commit()
    # две собаки и один помёт в этом питомнике
    db_session.add_all([
        Dog(breed_id=breed_id, name="A", sex=SexEnum.male, kennel_id=kennel.id),
        Dog(breed_id=breed_id, name="B", sex=SexEnum.female, kennel_id=kennel.id),
        Litter(kennel_id=kennel.id, breed_id=breed_id),
    ])
    await db_session.commit()

    r = await client.get(f"/kennels/{kennel.id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["is_verified"] is True
    assert body["dogs_count"] == 2
    assert body["litters_count"] == 1


async def test_litter_parents_and_puppies(client, db_session):
    breed_id = await _breed_id(db_session)
    owner_id = await _owner(db_session)
    kennel = Kennel(owner_id=owner_id, name="K2")
    db_session.add(kennel)
    await db_session.commit()
    sire = Dog(breed_id=breed_id, name="Папа", sex=SexEnum.male)
    dam = Dog(breed_id=breed_id, name="Мама", sex=SexEnum.female)
    db_session.add_all([sire, dam])
    await db_session.commit()
    litter = Litter(
        kennel_id=kennel.id, breed_id=breed_id,
        father_id=sire.id, mother_id=dam.id,
    )
    db_session.add(litter)
    await db_session.commit()
    # щенок, привязанный к помёту
    puppy = Dog(
        breed_id=breed_id, name="Щенок", sex=SexEnum.male, litter_id=litter.id
    )
    db_session.add(puppy)
    await db_session.commit()

    r = await client.get(f"/litters/{litter.id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["father"]["name"] == "Папа"
    assert body["mother"]["name"] == "Мама"

    r = await client.get(f"/litters/{litter.id}/puppies")
    assert r.status_code == 200
    names = [d["name"] for d in r.json()]
    assert "Щенок" in names
