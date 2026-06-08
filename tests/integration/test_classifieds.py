"""
Интеграция: карточка объявления (GET /classifieds/{id}).

Регрессия: эндпоинт инкрементирует views_count атомарным bulk-UPDATE и
коммитит внутри той же сессии, где лежит уже загруженный объект. Раньше
это экспайрило атрибуты объекта (в т.ч. updated_at от onupdate), и при
сериализации ответа FastAPI пытался синхронно дочитать их из БД →
MissingGreenlet → 500. Тест держит контракт: 200 + корректный инкремент
просмотров + сериализуемые images.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.models.classified import (
    Classified,
    ClassifiedCategory,
    ClassifiedImage,
    ClassifiedPriceKind,
    ClassifiedStatus,
)
from app.models.dog import SexEnum
from app.models.file import UploadedFile
from app.models.reference import Breed
from app.models.user import User


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


async def test_get_classified_increments_views(client, db_session):
    breed_id = await _breed_id(db_session)
    owner_id = await _owner(db_session)
    c = Classified(
        author_id=owner_id,
        category=ClassifiedCategory.mating,
        title="Тестовое объявление",
        description="Описание объявления",
        price=None,
        price_kind=ClassifiedPriceKind.negotiable,
        city="Казань",
        status=ClassifiedStatus.active,
    )
    db_session.add(c)
    await db_session.commit()
    # Привязываем изображение, чтобы задействовать relationship images в ответе.
    file_id = await _file(db_session)
    db_session.add(
        ClassifiedImage(
            classified_id=c.id, file_id=file_id, position=0, is_primary=True
        )
    )
    await db_session.commit()

    r = await client.get(f"/classifieds/{c.id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == str(c.id)
    assert body["views_count"] == 1
    assert body["images"][0]["file_id"] == str(file_id)

    # Повторный заход — счётчик растёт дальше.
    r2 = await client.get(f"/classifieds/{c.id}")
    assert r2.status_code == 200, r2.text
    assert r2.json()["views_count"] == 2


async def test_get_classified_not_found(client):
    r = await client.get(f"/classifieds/{uuid.uuid4()}")
    assert r.status_code == 404


async def _make_classified(db_session, owner_id, *, city, sex):
    """Активное объявление с заданным полом (или NULL)."""
    c = Classified(
        author_id=owner_id,
        category=ClassifiedCategory.adult_sale,
        title=f"Объявление {sex}",
        description="Описание объявления для фильтра по полу",
        price=None,
        price_kind=ClassifiedPriceKind.negotiable,
        city=city,
        sex=sex,
        status=ClassifiedStatus.active,
    )
    db_session.add(c)
    await db_session.commit()
    return c.id


async def test_list_classifieds_filter_by_sex(client, db_session):
    """
    Фильтр ?sex=male|female (запрос фронта 2026-06-08). Уникальный city
    изолирует выборку от сидов/dev-данных, поэтому total и состав items
    проверяемы точно. NULL-объявления под точечный фильтр не попадают.
    """
    owner_id = await _owner(db_session)
    # Уникальный город — «песочница» только для этого теста.
    city = f"SexFilterCity{uuid.uuid4().hex[:8]}"
    male_id = await _make_classified(db_session, owner_id, city=city, sex=SexEnum.male)
    female_id = await _make_classified(db_session, owner_id, city=city, sex=SexEnum.female)
    null_id = await _make_classified(db_session, owner_id, city=city, sex=None)

    # ?sex=male → только male, NULL и female исключены.
    r = await client.get("/classifieds", params={"city": city, "sex": "male"})
    assert r.status_code == 200, r.text
    body = r.json()
    ids = {item["id"] for item in body["items"]}
    assert ids == {str(male_id)}
    assert body["total"] == 1
    assert body["items"][0]["sex"] == "male"

    # ?sex=female → только female.
    r = await client.get("/classifieds", params={"city": city, "sex": "female"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert {item["id"] for item in body["items"]} == {str(female_id)}
    assert body["total"] == 1

    # Без sex → все три, включая NULL.
    r = await client.get("/classifieds", params={"city": city})
    assert r.status_code == 200, r.text
    body = r.json()
    assert {item["id"] for item in body["items"]} == {
        str(male_id), str(female_id), str(null_id)
    }
    assert body["total"] == 3
