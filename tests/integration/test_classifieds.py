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
    AnimalAvailability,
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


async def _make_classified_av(db_session, owner_id, *, city, availability):
    """Активное объявление с заданной доступностью."""
    c = Classified(
        author_id=owner_id,
        category=ClassifiedCategory.adult_sale,
        title=f"Объявление {availability.value}",
        description="Описание объявления для фильтра по доступности",
        price=None,
        price_kind=ClassifiedPriceKind.negotiable,
        city=city,
        status=ClassifiedStatus.active,
        availability=availability,
    )
    db_session.add(c)
    await db_session.commit()
    return c.id


async def test_list_classifieds_filter_by_availability(client, db_session):
    """
    Фильтр ?availability=available|reserved|sold. Уникальный city
    изолирует выборку от сидов/dev-данных. Без фильтра отдаются все
    объявления (включая забронированных/проданных) — availability и
    status независимы.
    """
    owner_id = await _owner(db_session)
    city = f"AvailFilterCity{uuid.uuid4().hex[:8]}"
    free_id = await _make_classified_av(
        db_session, owner_id, city=city, availability=AnimalAvailability.available
    )
    reserved_id = await _make_classified_av(
        db_session, owner_id, city=city, availability=AnimalAvailability.reserved
    )
    sold_id = await _make_classified_av(
        db_session, owner_id, city=city, availability=AnimalAvailability.sold
    )

    # ?availability=available → только свободные.
    r = await client.get(
        "/classifieds", params={"city": city, "availability": "available"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert {item["id"] for item in body["items"]} == {str(free_id)}
    assert body["total"] == 1
    assert body["items"][0]["availability"] == "available"

    # ?availability=sold → только проданные.
    r = await client.get(
        "/classifieds", params={"city": city, "availability": "sold"}
    )
    assert {item["id"] for item in r.json()["items"]} == {str(sold_id)}

    # Без фильтра → все три (active, но разной доступности).
    r = await client.get("/classifieds", params={"city": city})
    body = r.json()
    assert {item["id"] for item in body["items"]} == {
        str(free_id), str(reserved_id), str(sold_id)
    }
    assert body["total"] == 3


async def test_default_availability_is_available(client, db_session):
    """Новое объявление без явной availability → 'available' (server_default)."""
    owner_id = await _owner(db_session)
    c = Classified(
        author_id=owner_id,
        category=ClassifiedCategory.adult_sale,
        title="Объявление без явной доступности",
        description="Проверяем дефолт availability",
        price=None,
        price_kind=ClassifiedPriceKind.negotiable,
        city="Самара",
        status=ClassifiedStatus.active,
    )
    db_session.add(c)
    await db_session.commit()

    r = await client.get(f"/classifieds/{c.id}")
    assert r.status_code == 200, r.text
    assert r.json()["availability"] == "available"


async def test_author_changes_availability_via_put(client, db_session):
    """
    Автор меняет доступность через PUT (available → reserved). После этого
    объявление выпадает из фильтра ?availability=available и попадает в
    ?availability=reserved. Право меняет _check_owner — подменяем
    get_current_user на автора.
    """
    from app.dependencies import get_current_user
    from app.main import app

    owner = User(
        email=f"av_{uuid.uuid4().hex[:8]}@example.com", hashed_password="x"
    )
    db_session.add(owner)
    await db_session.commit()
    # is_admin(user) обходит user.roles — это lazy-relationship. В реальном
    # get_current_user роли грузятся через selectinload; здесь подгружаем
    # их явно в async-контексте, иначе ленивая загрузка внутри обработчика
    # упадёт MissingGreenlet.
    await db_session.refresh(owner, attribute_names=["roles"])

    city = f"AvailPutCity{uuid.uuid4().hex[:8]}"
    c = Classified(
        author_id=owner.id,
        category=ClassifiedCategory.adult_sale,
        title="Объявление под бронь",
        description="Меняем доступность через PUT",
        price=None,
        price_kind=ClassifiedPriceKind.negotiable,
        city=city,
        status=ClassifiedStatus.active,
    )
    db_session.add(c)
    await db_session.commit()
    # Дефолт применился на INSERT.
    assert c.availability == AnimalAvailability.available

    app.dependency_overrides[get_current_user] = lambda: owner
    r = await client.put(
        f"/classifieds/{c.id}", json={"availability": "reserved"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["availability"] == "reserved"

    # Свободных в этом городе больше нет, забронированный — один.
    r = await client.get(
        "/classifieds", params={"city": city, "availability": "available"}
    )
    assert r.json()["total"] == 0
    r = await client.get(
        "/classifieds", params={"city": city, "availability": "reserved"}
    )
    assert {item["id"] for item in r.json()["items"]} == {str(c.id)}
