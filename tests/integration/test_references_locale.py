"""
Интеграционные тесты локализации справочников по Accept-Language.

Контракт (спека 2026-06-11, фронт show-ring):
- ответ /references/* отдаёт name/description на языке заголовка;
- язык не определён → русский (канонический, лежит в name);
- en при пустом name_en → фолбэк на русский;
- поиск пород работает и по name, и по name_en независимо от локали;
- сортировка пород для en — по coalesce(name_en, name).
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.reference import AnimalType, Breed, ShowClass

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def reference_data(db_session: AsyncSession) -> dict:
    """Минимальный набор справочников с переводами и без."""
    # Код вида уникален глобально, а dev-БД уже содержит "dog" из сида —
    # берём тестовый код, чтобы тест работал и на засеянной базе.
    animal = AnimalType(code="test-i18n-dog", name="Собака", name_en="Dog")
    db_session.add(animal)
    await db_session.flush()

    # Русский порядок: Аки < Бета; английский — инвертирован
    # (Alpha < Zeta), чтобы проверить сортировку по coalesce(name_en, name).
    breed_translated = Breed(
        animal_type_id=animal.id, code="aki", name="Аки", name_en="Zeta Hound"
    )
    breed_inverted = Breed(
        animal_type_id=animal.id, code="beta", name="Бета", name_en="Alpha Hound"
    )
    # Перевода нет — en должен фолбэкнуться на русский name.
    breed_untranslated = Breed(
        animal_type_id=animal.id, code="gamma", name="Гамма"
    )
    show_class = ShowClass(
        animal_type_id=animal.id,
        code="open",
        name="Открытый класс",
        name_en="Open Class",
        age_from_months=15,
        description="С 15 месяцев",
        description_en="From 15 months",
    )
    db_session.add_all(
        [breed_translated, breed_inverted, breed_untranslated, show_class]
    )
    await db_session.commit()
    return {"animal": animal}


async def _breed_names(
    client: AsyncClient, animal_id, headers: dict | None = None
) -> list[str]:
    resp = await client.get(
        "/references/breeds",
        params={"animal_type_id": str(animal_id)},
        headers=headers or {},
    )
    assert resp.status_code == 200
    return [item["name"] for item in resp.json()["items"]]


async def test_breeds_default_russian(client: AsyncClient, reference_data: dict):
    """Без заголовка — русские имена в русском порядке."""
    names = await _breed_names(client, reference_data["animal"].id)
    assert names == ["Аки", "Бета", "Гамма"]


async def test_breeds_english_with_fallback_and_order(
    client: AsyncClient, reference_data: dict
):
    """en: переводы + фолбэк на русский, порядок по coalesce(name_en, name)."""
    names = await _breed_names(
        client,
        reference_data["animal"].id,
        headers={"Accept-Language": "en-US,en;q=0.9"},
    )
    # Alpha Hound < Zeta Hound < Гамма (латиница раньше кириллицы).
    assert names == ["Alpha Hound", "Zeta Hound", "Гамма"]


async def test_breeds_unsupported_language_falls_back_to_russian(
    client: AsyncClient, reference_data: dict
):
    names = await _breed_names(
        client, reference_data["animal"].id, headers={"Accept-Language": "fr-FR"}
    )
    assert names == ["Аки", "Бета", "Гамма"]


async def test_breed_search_matches_english_name(
    client: AsyncClient, reference_data: dict
):
    """Поиск находит породу по английскому имени даже при русской локали."""
    resp = await client.get(
        "/references/breeds",
        params={
            "animal_type_id": str(reference_data["animal"].id),
            "search": "alpha",
        },
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert [item["code"] for item in items] == ["beta"]


async def test_breed_detail_localized(client: AsyncClient, reference_data: dict):
    resp = await client.get(
        "/references/breeds",
        params={"animal_type_id": str(reference_data["animal"].id), "search": "Аки"},
    )
    breed_id = resp.json()["items"][0]["id"]

    detail_en = await client.get(
        f"/references/breeds/{breed_id}", headers={"Accept-Language": "en"}
    )
    assert detail_en.status_code == 200
    assert detail_en.json()["name"] == "Zeta Hound"

    detail_ru = await client.get(f"/references/breeds/{breed_id}")
    assert detail_ru.json()["name"] == "Аки"


async def test_show_classes_localize_name_and_description(
    client: AsyncClient, reference_data: dict
):
    params = {"animal_type_id": str(reference_data["animal"].id)}

    resp_ru = await client.get("/references/show-classes", params=params)
    assert resp_ru.status_code == 200
    open_ru = resp_ru.json()[0]
    assert open_ru["name"] == "Открытый класс"
    assert open_ru["description"] == "С 15 месяцев"

    resp_en = await client.get(
        "/references/show-classes",
        params=params,
        headers={"Accept-Language": "en"},
    )
    open_en = resp_en.json()[0]
    assert open_en["name"] == "Open Class"
    assert open_en["description"] == "From 15 months"
