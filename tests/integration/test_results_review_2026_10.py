"""
Интеграция: результаты ринга (ревью 2026-10-06, BE-26).

- Судья результата — назначенный на породу судья, а не тот, кто вводит
  оценку: секретарь/организатор раньше записывался «судьёй» в результат
  и в титулы (а оттуда — в дипломы и сертификаты).
- Одно место в классе (с учётом пола) — у одной собаки: два «первых места»
  давали два CW/CAC.
- Оценку и место можно явно сбросить (null), титулы при этом отзываются.
- Судья правит результаты только своих пород/групп.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.models.dog import Dog, SexEnum
from app.models.reference import Breed, Grade, ShowClass, ShowRank
from app.models.result import DogTitle
from app.models.show import Show, ShowEntry, ShowJudge, ShowStatus
from app.models.user import User
from app.services import result as result_svc


async def _first(db_session, model, *where):
    stmt = select(model)
    for cond in where:
        stmt = stmt.where(cond)
    obj = (await db_session.execute(stmt.limit(1))).scalars().first()
    if obj is None:
        pytest.skip(f"нет {model.__name__} в сидах — пропускаем")
    return obj


async def _user(db_session) -> User:
    u = User(email=f"rr_{uuid.uuid4().hex[:8]}@example.com", hashed_password="x")
    db_session.add(u)
    await db_session.commit()
    return u


async def _setup(db_session, *, dogs: int = 1, judge_breed: bool = True):
    breed = await _first(db_session, Breed)
    other_breed = await _first(db_session, Breed, Breed.id != breed.id)
    rank = await _first(db_session, ShowRank)
    organizer, judge = await _user(db_session), await _user(db_session)
    cls = ShowClass(
        animal_type_id=breed.animal_type_id, code=f"OPEN{uuid.uuid4().hex[:4]}",
        name="Открытый", age_from_months=15, age_to_months=None,
        can_receive_cac=True,
    )
    db_session.add(cls)
    dog_rows = [
        Dog(
            breed_id=breed.id, name=f"Пёс {i}", sex=SexEnum.male,
            date_of_birth=date.today() - timedelta(days=900),
        )
        for i in range(dogs)
    ]
    db_session.add_all(dog_rows)
    await db_session.commit()
    show = Show(
        organizer_id=organizer.id, name="Ринг", rank_id=rank.id,
        date_start=date.today(), status=ShowStatus.in_progress,
    )
    db_session.add(show)
    await db_session.commit()
    db_session.add(ShowJudge(
        show_id=show.id, judge_id=judge.id,
        breed_id=breed.id if judge_breed else other_breed.id,
    ))
    entries = [
        ShowEntry(show_id=show.id, dog_id=d.id, show_class_id=cls.id,
                  registered_by=organizer.id)
        for d in dog_rows
    ]
    db_session.add_all(entries)
    await db_session.commit()
    excellent = await _first(
        db_session, Grade, Grade.code == "excellent",
        Grade.animal_type_id == breed.animal_type_id,
    )
    return show, organizer, judge, entries, excellent


async def _upsert(db_session, entry, user, *, is_admin=False, **fields):
    params = dict(grade_id=None, placement=None, critique=None)
    params.update(fields)
    return await result_svc.upsert_class_result(
        db_session, show_entry_id=entry.id, user_id=user.id, is_admin=is_admin,
        **params,
    )


async def test_organizer_entry_records_assigned_judge(db_session):
    show, organizer, judge, (entry,), excellent = await _setup(db_session)
    result = await _upsert(
        db_session, entry, organizer, grade_id=excellent.id, placement=1
    )
    assert result.judge_id == judge.id
    titles = (
        await db_session.execute(select(DogTitle).where(DogTitle.show_id == show.id))
    ).scalars().all()
    assert titles and all(t.judge_id == judge.id for t in titles)


async def test_placement_is_unique_within_class(db_session):
    _, organizer, _, entries, excellent = await _setup(db_session, dogs=2)
    await _upsert(db_session, entries[0], organizer, grade_id=excellent.id, placement=1)
    with pytest.raises(ValueError, match="placement_taken"):
        await _upsert(
            db_session, entries[1], organizer, grade_id=excellent.id, placement=1
        )


async def test_placement_can_be_cleared(db_session):
    _, organizer, _, (entry,), excellent = await _setup(db_session)
    result = await _upsert(
        db_session, entry, organizer, grade_id=excellent.id, placement=1
    )
    assert result.is_class_winner
    result = await _upsert(
        db_session, entry, organizer, placement=None, clear={"placement"}
    )
    assert result.placement is None
    assert result.is_class_winner is False
    assert not result.titles_cache


async def test_judge_cannot_edit_other_breed(db_session):
    _, _, judge, (entry,), excellent = await _setup(db_session, judge_breed=False)
    with pytest.raises(ValueError, match="forbidden"):
        await _upsert(db_session, entry, judge, grade_id=excellent.id)


async def test_judge_can_edit_own_breed(db_session):
    _, _, judge, (entry,), excellent = await _setup(db_session)
    result = await _upsert(db_session, entry, judge, grade_id=excellent.id)
    assert result.judge_id == judge.id


async def test_judge_cannot_delete_other_breed_result(db_session):
    show, organizer, judge, (entry,), excellent = await _setup(
        db_session, judge_breed=False
    )
    result = await _upsert(db_session, entry, organizer, grade_id=excellent.id)
    with pytest.raises(ValueError, match="forbidden"):
        await result_svc.delete_result(
            db_session, show_id=show.id, result_id=result.id,
            user_id=judge.id, is_admin=False,
        )
