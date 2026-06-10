"""
Интеграция: правка по review 2026-06-10 — отзыв титулов при исправлении
результата.

Сценарий: судья ошибочно ввёл «отлично / 1 место» → собака получила CW+CAC
(dog_titles + titles_cache). Судья исправляет оценку на «очень хорошо» —
титулы класса обязаны отзываться (это юридически значимые данные РКФ,
ошибочный CAC влияет на чемпионство). Раньше _apply_class_titles только
добавлял, и титул переживал свой результат.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.models.dog import Dog, SexEnum
from app.models.reference import Breed, Grade, ShowClass, ShowRank
from app.models.result import DogTitle
from app.models.show import Show, ShowEntry, ShowStatus
from app.models.user import User
from app.services import result as result_svc


async def _first(db_session, model, **filters):
    stmt = select(model)
    for field, value in filters.items():
        stmt = stmt.where(getattr(model, field) == value)
    obj = (await db_session.execute(stmt.limit(1))).scalars().first()
    if obj is None:
        pytest.skip(f"нет {model.__name__} в сидах — пропускаем")
    return obj


async def _titles_codes(db_session, dog_id, show_id) -> set[str]:
    """Коды титулов собаки на выставке (join через Title не нужен —
    хватит сверки количества; коды берём из titles_cache в тесте)."""
    rows = (
        await db_session.execute(
            select(DogTitle).where(
                DogTitle.dog_id == dog_id, DogTitle.show_id == show_id
            )
        )
    ).scalars().all()
    return {str(r.title_id) for r in rows}


async def test_correcting_result_revokes_stale_class_titles(db_session):
    breed = await _first(db_session, Breed)
    rank = await _first(db_session, ShowRank)
    excellent = await _first(
        db_session, Grade,
        code="excellent", animal_type_id=breed.animal_type_id,
    )
    very_good = await _first(
        db_session, Grade,
        code="very-good", animal_type_id=breed.animal_type_id,
    )

    judge = User(
        email=f"judge_{uuid.uuid4().hex[:8]}@example.com", hashed_password="x"
    )
    db_session.add(judge)
    cls = ShowClass(
        animal_type_id=breed.animal_type_id,
        code=f"OPEN{uuid.uuid4().hex[:4]}",
        name="Открытый",
        age_from_months=15,
        age_to_months=None,
        can_receive_cac=True,
    )
    db_session.add(cls)
    dog = Dog(
        breed_id=breed.id, name="Чемпион", sex=SexEnum.male,
        date_of_birth=date.today() - timedelta(days=900),
    )
    db_session.add(dog)
    await db_session.commit()

    show = Show(
        organizer_id=judge.id, name="Ринговая", rank_id=rank.id,
        date_start=date.today(), status=ShowStatus.in_progress,
    )
    db_session.add(show)
    await db_session.commit()
    entry = ShowEntry(
        show_id=show.id, dog_id=dog.id, show_class_id=cls.id,
        registered_by=judge.id,
    )
    db_session.add(entry)
    await db_session.commit()

    # 1. Ошибочный ввод: отлично / 1 место → CW (+CAC, если титулы в сидах).
    result = await result_svc.upsert_class_result(
        db_session,
        show_entry_id=entry.id,
        user_id=judge.id,
        is_admin=False,
        grade_id=excellent.id,
        placement=1,
        critique=None,
    )
    assert result.is_class_winner is True
    granted_codes = {item["code"] for item in (result.titles_cache or [])}
    if not granted_codes:
        pytest.skip("титулы (cw/cac) не заведены в сидах — пропускаем")
    titles_before = await _titles_codes(db_session, dog.id, show.id)
    assert titles_before  # строки в dog_titles появились

    # 2. Исправление: очень хорошо (placement не трогаем) → титулы класса
    # отзываются и из dog_titles, и из titles_cache.
    result = await result_svc.upsert_class_result(
        db_session,
        show_entry_id=entry.id,
        user_id=judge.id,
        is_admin=False,
        grade_id=very_good.id,
        placement=None,
        critique=None,
    )
    assert result.is_class_winner is False
    cache_codes = {item["code"] for item in (result.titles_cache or [])}
    assert cache_codes == set(), f"titles_cache не очищен: {cache_codes}"
    titles_after = await _titles_codes(db_session, dog.id, show.id)
    assert titles_after == set(), "строки dog_titles пережили исправление"
