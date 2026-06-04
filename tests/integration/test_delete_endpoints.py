"""
Интеграция: DELETE-эндпоинты для dogs, litters, results, ads и classifieds.

Покрывает добавленные операции удаления:
- успешное удаление владельцем (204) и исчезновение записи;
- отказ постороннему (403);
- побочные эффекты каскадов/SET NULL (щенок переживает удаление помёта;
  результат отзывает титулы; soft-vs-hard для объявления).

Данные вставляем напрямую через db_session (как в остальных интеграционных
тестах), владельца — регистрируем через API, чтобы получить рабочий JWT.
Справочные сущности (порода/ранг/класс/титул) берём из dev-сидов; если их
нет — тест skip'ается, а не падает.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.ad import AdBanner, AdCampaign, BannerPlacement
from app.models.classified import (
    Classified,
    ClassifiedCategory,
    ClassifiedPriceKind,
    ClassifiedStatus,
)
from app.models.dog import Dog, SexEnum
from app.models.kennel import Kennel
from app.models.litter import Litter
from app.models.reference import Breed, ShowClass, ShowRank, Title
from app.models.result import DogTitle, ShowResult
from app.models.show import Show, ShowEntry, ShowStatus

PASSWORD = "secret123"


async def _make_user(client) -> tuple[uuid.UUID, str]:
    """Регистрирует и логинит пользователя, возвращает (id, access_token)."""
    email = f"itest_{uuid.uuid4().hex[:10]}@example.com"
    await client.post(
        "/auth/register", json={"email": email, "password": PASSWORD}
    )
    r = await client.post(
        "/auth/login", json={"email": email, "password": PASSWORD}
    )
    access = r.json()["access_token"]
    me = await client.get(
        "/users/me", headers={"Authorization": f"Bearer {access}"}
    )
    return uuid.UUID(me.json()["id"]), access


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _seed_id(db_session, model) -> uuid.UUID:
    sid = (
        await db_session.execute(select(model.id).limit(1))
    ).scalar_one_or_none()
    if sid is None:
        pytest.skip(f"в БД нет сидов {model.__name__} — пропускаем")
    return sid


# ---------------------------------------------------------------------
# dogs
# ---------------------------------------------------------------------


async def test_delete_dog_owner_ok(client, db_session):
    breed_id = await _seed_id(db_session, Breed)
    uid, token = await _make_user(client)
    kennel = Kennel(owner_id=uid, name="DelK")
    db_session.add(kennel)
    await db_session.commit()
    dog = Dog(
        breed_id=breed_id, name="НаУдаление", sex=SexEnum.male,
        kennel_id=kennel.id,
    )
    db_session.add(dog)
    await db_session.commit()

    r = await client.delete(f"/dogs/{dog.id}", headers=_auth(token))
    assert r.status_code == 204, r.text
    assert await db_session.get(Dog, dog.id) is None
    # И публично карточка больше не отдаётся.
    assert (await client.get(f"/dogs/{dog.id}")).status_code == 404


async def test_delete_dog_foreign_forbidden(client, db_session):
    breed_id = await _seed_id(db_session, Breed)
    owner_id, _ = await _make_user(client)
    _other_id, other_token = await _make_user(client)
    kennel = Kennel(owner_id=owner_id, name="DelK2")
    db_session.add(kennel)
    await db_session.commit()
    dog = Dog(
        breed_id=breed_id, name="Чужая", sex=SexEnum.female, kennel_id=kennel.id
    )
    db_session.add(dog)
    await db_session.commit()

    r = await client.delete(f"/dogs/{dog.id}", headers=_auth(other_token))
    assert r.status_code == 403, r.text
    assert await db_session.get(Dog, dog.id) is not None


async def test_delete_dog_unknown_404(client):
    _uid, token = await _make_user(client)
    r = await client.delete(f"/dogs/{uuid.uuid4()}", headers=_auth(token))
    assert r.status_code == 404


# ---------------------------------------------------------------------
# litters
# ---------------------------------------------------------------------


async def test_delete_litter_keeps_puppy(client, db_session):
    breed_id = await _seed_id(db_session, Breed)
    uid, token = await _make_user(client)
    kennel = Kennel(owner_id=uid, name="DelLitterK")
    db_session.add(kennel)
    await db_session.commit()
    litter = Litter(kennel_id=kennel.id, breed_id=breed_id)
    db_session.add(litter)
    await db_session.commit()
    puppy = Dog(
        breed_id=breed_id, name="Выживший", sex=SexEnum.male,
        litter_id=litter.id,
    )
    db_session.add(puppy)
    await db_session.commit()

    r = await client.delete(f"/litters/{litter.id}", headers=_auth(token))
    assert r.status_code == 204, r.text
    assert await db_session.get(Litter, litter.id) is None
    # Щенок переживает удаление помёта (dogs.litter_id → SET NULL).
    survived = await db_session.get(Dog, puppy.id)
    assert survived is not None
    await db_session.refresh(survived)
    assert survived.litter_id is None


async def test_delete_litter_foreign_forbidden(client, db_session):
    breed_id = await _seed_id(db_session, Breed)
    owner_id, _ = await _make_user(client)
    _other_id, other_token = await _make_user(client)
    kennel = Kennel(owner_id=owner_id, name="DelLitterK2")
    db_session.add(kennel)
    await db_session.commit()
    litter = Litter(kennel_id=kennel.id, breed_id=breed_id)
    db_session.add(litter)
    await db_session.commit()

    r = await client.delete(
        f"/litters/{litter.id}", headers=_auth(other_token)
    )
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------------
# results (с отзывом титулов)
# ---------------------------------------------------------------------


async def test_delete_result_revokes_titles(client, db_session):
    breed_id = await _seed_id(db_session, Breed)
    rank_id = await _seed_id(db_session, ShowRank)
    class_id = await _seed_id(db_session, ShowClass)
    title_id = await _seed_id(db_session, Title)
    uid, token = await _make_user(client)

    show = Show(
        organizer_id=uid, rank_id=rank_id, name="DelResShow",
        date_start=date.today(), status=ShowStatus.in_progress,
    )
    dog = Dog(breed_id=breed_id, name="Чемпион", sex=SexEnum.male)
    db_session.add_all([show, dog])
    await db_session.commit()
    entry = ShowEntry(
        show_id=show.id, dog_id=dog.id, show_class_id=class_id,
        registered_by=uid,
    )
    db_session.add(entry)
    await db_session.commit()
    result = ShowResult(show_entry_id=entry.id)
    title = DogTitle(
        dog_id=dog.id, title_id=title_id, show_id=show.id,
        date_earned=date.today(),
    )
    db_session.add_all([result, title])
    await db_session.commit()

    r = await client.delete(
        f"/shows/{show.id}/results/{result.id}", headers=_auth(token)
    )
    assert r.status_code == 204, r.text
    assert await db_session.get(ShowResult, result.id) is None
    # Титул отозван вместе с результатом.
    left = (
        await db_session.execute(
            select(DogTitle).where(
                DogTitle.dog_id == dog.id, DogTitle.show_id == show.id
            )
        )
    ).scalars().all()
    assert left == []


async def test_delete_result_foreign_forbidden(client, db_session):
    breed_id = await _seed_id(db_session, Breed)
    rank_id = await _seed_id(db_session, ShowRank)
    class_id = await _seed_id(db_session, ShowClass)
    organizer_id, _ = await _make_user(client)
    _other_id, other_token = await _make_user(client)

    show = Show(
        organizer_id=organizer_id, rank_id=rank_id, name="DelResShow2",
        date_start=date.today(), status=ShowStatus.in_progress,
    )
    dog = Dog(breed_id=breed_id, name="Пёс2", sex=SexEnum.male)
    db_session.add_all([show, dog])
    await db_session.commit()
    entry = ShowEntry(
        show_id=show.id, dog_id=dog.id, show_class_id=class_id,
        registered_by=organizer_id,
    )
    db_session.add(entry)
    await db_session.commit()
    result = ShowResult(show_entry_id=entry.id)
    db_session.add(result)
    await db_session.commit()

    # Посторонний (не организатор/судья/admin) → 403.
    r = await client.delete(
        f"/shows/{show.id}/results/{result.id}", headers=_auth(other_token)
    )
    assert r.status_code == 403, r.text
    assert await db_session.get(ShowResult, result.id) is not None


# ---------------------------------------------------------------------
# ads (campaigns + banners)
# ---------------------------------------------------------------------


async def test_delete_campaign_cascades_banner(client, db_session):
    uid, token = await _make_user(client)
    camp = AdCampaign(
        advertiser_id=uid, name="DelCamp", budget=Decimal("100.00"),
        date_start=date.today(), date_end=date.today(),
    )
    db_session.add(camp)
    await db_session.commit()
    banner = AdBanner(
        campaign_id=camp.id, target_url="https://example.com",
        placement=BannerPlacement.sidebar,
    )
    db_session.add(banner)
    await db_session.commit()

    r = await client.delete(
        f"/ads/campaigns/{camp.id}", headers=_auth(token)
    )
    assert r.status_code == 204, r.text
    assert await db_session.get(AdCampaign, camp.id) is None
    # Баннер ушёл по каскаду.
    assert await db_session.get(AdBanner, banner.id) is None


async def test_delete_banner_owner_ok(client, db_session):
    uid, token = await _make_user(client)
    camp = AdCampaign(
        advertiser_id=uid, name="DelBanCamp", budget=Decimal("50.00"),
        date_start=date.today(), date_end=date.today(),
    )
    db_session.add(camp)
    await db_session.commit()
    banner = AdBanner(
        campaign_id=camp.id, target_url="https://example.com",
        placement=BannerPlacement.top,
    )
    db_session.add(banner)
    await db_session.commit()

    r = await client.delete(
        f"/ads/banners/{banner.id}", headers=_auth(token)
    )
    assert r.status_code == 204, r.text
    assert await db_session.get(AdBanner, banner.id) is None
    # Кампания на месте.
    assert await db_session.get(AdCampaign, camp.id) is not None


async def test_delete_campaign_foreign_forbidden(client, db_session):
    owner_id, _ = await _make_user(client)
    _other_id, other_token = await _make_user(client)
    camp = AdCampaign(
        advertiser_id=owner_id, name="DelCampF", budget=Decimal("10.00"),
        date_start=date.today(), date_end=date.today(),
    )
    db_session.add(camp)
    await db_session.commit()

    r = await client.delete(
        f"/ads/campaigns/{camp.id}", headers=_auth(other_token)
    )
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------------
# classifieds (soft по умолчанию / hard по флагу)
# ---------------------------------------------------------------------


async def test_delete_classified_soft_default(client, db_session):
    uid, token = await _make_user(client)
    c = Classified(
        author_id=uid, category=ClassifiedCategory.other, title="t",
        description="d", price_kind=ClassifiedPriceKind.negotiable,
    )
    db_session.add(c)
    await db_session.commit()

    r = await client.delete(f"/classifieds/{c.id}", headers=_auth(token))
    assert r.status_code == 204, r.text
    # Soft: строка остаётся, статус closed.
    survived = await db_session.get(Classified, c.id)
    assert survived is not None
    await db_session.refresh(survived)
    assert survived.status == ClassifiedStatus.closed


async def test_delete_classified_hard_removes_row(client, db_session):
    uid, token = await _make_user(client)
    c = Classified(
        author_id=uid, category=ClassifiedCategory.other, title="t2",
        description="d2", price_kind=ClassifiedPriceKind.negotiable,
    )
    db_session.add(c)
    await db_session.commit()

    r = await client.delete(
        f"/classifieds/{c.id}?hard=true", headers=_auth(token)
    )
    assert r.status_code == 204, r.text
    # Hard: строки больше нет в БД.
    assert await db_session.get(Classified, c.id) is None
