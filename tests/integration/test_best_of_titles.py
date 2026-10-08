"""
Интеграция: BOB / BIG / BIS (ревью 2026-10-06, BE-03 и BE-04).

BE-03: set_best_* обязаны соблюдать тот же статус-гейт, что и ввод
результата ринга (registration_closed / in_progress), и не присуждать
титулы недопущенным на чек-ине собакам.

BE-04: при перевыборе победителя титулы прежнего (BOB/CACIB/BIG/BIS)
отзываются — и из dog_titles, и из titles_cache. Раньше снимались только
флаги, и у двух собак одной породы оказывалось по титулу BOB.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.models.dog import Dog, SexEnum
from app.models.reference import Breed, ShowClass, ShowRank, Title
from app.models.result import DogTitle, ShowResult
from app.models.show import AttendanceStatus, Show, ShowEntry, ShowStatus
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


async def _codes(db_session, dog_id, show_id) -> set[str]:
    rows = await db_session.execute(
        select(Title.code)
        .join(DogTitle, DogTitle.title_id == Title.id)
        .where(DogTitle.dog_id == dog_id, DogTitle.show_id == show_id)
    )
    return set(rows.scalars().all())


async def _cache_codes(db_session, entry_id) -> set[str]:
    res = (
        await db_session.execute(
            select(ShowResult).where(ShowResult.show_entry_id == entry_id)
        )
    ).scalar_one()
    return {item["code"] for item in (res.titles_cache or [])}


async def _setup(db_session, *, status=ShowStatus.in_progress, checkin=False):
    breed = await _first(db_session, Breed, Breed.breed_group_id.is_not(None))
    for code in ("bob", "big", "bis"):
        await _first(
            db_session, Title,
            Title.code == code, Title.animal_type_id == breed.animal_type_id,
        )
    rank = await _first(db_session, ShowRank)

    organizer = User(
        email=f"org_{uuid.uuid4().hex[:8]}@example.com", hashed_password="x"
    )
    db_session.add(organizer)
    cls = ShowClass(
        animal_type_id=breed.animal_type_id,
        code=f"OPEN{uuid.uuid4().hex[:4]}",
        name="Открытый",
        age_from_months=15,
        age_to_months=None,
        can_receive_cac=True,
    )
    db_session.add(cls)
    dogs = [
        Dog(
            breed_id=breed.id, name=f"Собака {i}", sex=SexEnum.male,
            date_of_birth=date.today() - timedelta(days=900),
        )
        for i in range(2)
    ]
    db_session.add_all(dogs)
    await db_session.commit()

    show = Show(
        organizer_id=organizer.id, name="Ринговая", rank_id=rank.id,
        date_start=date.today(), status=ShowStatus.in_progress,
        checkin_enabled=checkin,
    )
    db_session.add(show)
    await db_session.commit()
    entries = [
        ShowEntry(
            show_id=show.id, dog_id=d.id, show_class_id=cls.id,
            registered_by=organizer.id,
        )
        for d in dogs
    ]
    db_session.add_all(entries)
    await db_session.commit()
    for e in entries:
        await result_svc.upsert_class_result(
            db_session, show_entry_id=e.id, user_id=organizer.id,
            is_admin=False, grade_id=None, placement=None, critique=None,
        )
    show.status = status
    await db_session.commit()
    return breed, show, organizer, dogs, entries


async def _bob(db_session, show, organizer, breed, entry):
    return await result_svc.set_best_of_breed(
        db_session, show_id=show.id, user_id=organizer.id, is_admin=False,
        breed_id=breed.id, winner_entry_id=entry.id,
        best_male_entry_id=None, best_female_entry_id=None,
        best_junior_entry_id=None, best_veteran_entry_id=None,
    )


async def test_reelecting_bob_revokes_previous_winner_titles(db_session):
    breed, show, org, dogs, entries = await _setup(db_session)

    await _bob(db_session, show, org, breed, entries[0])
    await result_svc.set_best_in_group(
        db_session, show_id=show.id, user_id=org.id, is_admin=False,
        breed_group_id=breed.breed_group_id, winner_entry_id=entries[0].id,
    )
    await result_svc.set_best_in_show(
        db_session, show_id=show.id, user_id=org.id, is_admin=False,
        winner_entry_id=entries[0].id,
    )
    assert {"bob", "big", "bis"} <= await _codes(db_session, dogs[0].id, show.id)

    # Перевыбор BOB: прежний победитель теряет BOB и каскадом BIG/BIS.
    await _bob(db_session, show, org, breed, entries[1])

    assert not {"bob", "big", "bis"} & await _codes(db_session, dogs[0].id, show.id)
    assert not {"bob", "big", "bis"} & await _cache_codes(db_session, entries[0].id)
    assert "bob" in await _codes(db_session, dogs[1].id, show.id)


async def test_reelecting_big_revokes_previous_big_and_bis(db_session):
    breed, show, org, dogs, entries = await _setup(db_session)
    await _bob(db_session, show, org, breed, entries[0])
    await result_svc.set_best_in_group(
        db_session, show_id=show.id, user_id=org.id, is_admin=False,
        breed_group_id=breed.breed_group_id, winner_entry_id=entries[0].id,
    )
    await result_svc.set_best_in_show(
        db_session, show_id=show.id, user_id=org.id, is_admin=False,
        winner_entry_id=entries[0].id,
    )

    # Повторный выбор того же BIG снимает и тут же возвращает BIG, но BIS
    # (уровнем выше) отзывается — инвариант BIS ⊆ BIG.
    await result_svc.set_best_in_group(
        db_session, show_id=show.id, user_id=org.id, is_admin=False,
        breed_group_id=breed.breed_group_id, winner_entry_id=entries[0].id,
    )
    codes = await _codes(db_session, dogs[0].id, show.id)
    assert "big" in codes and "bob" in codes
    assert "bis" not in codes
    assert "bis" not in await _cache_codes(db_session, entries[0].id)


@pytest.mark.parametrize(
    "status",
    [ShowStatus.completed, ShowStatus.registration_open, ShowStatus.draft],
)
async def test_best_of_breed_rejected_outside_results_window(db_session, status):
    breed, show, org, _dogs, entries = await _setup(db_session, status=status)
    with pytest.raises(ValueError, match="show_not_in_progress"):
        await _bob(db_session, show, org, breed, entries[0])


async def test_best_in_show_rejected_after_publication(db_session):
    breed, show, org, _dogs, entries = await _setup(db_session)
    await _bob(db_session, show, org, breed, entries[0])
    await result_svc.set_best_in_group(
        db_session, show_id=show.id, user_id=org.id, is_admin=False,
        breed_group_id=breed.breed_group_id, winner_entry_id=entries[0].id,
    )
    show.status = ShowStatus.completed
    await db_session.commit()
    with pytest.raises(ValueError, match="show_not_in_progress"):
        await result_svc.set_best_in_show(
            db_session, show_id=show.id, user_id=org.id, is_admin=False,
            winner_entry_id=entries[0].id,
        )


async def test_best_of_breed_rejected_for_absent_entry(db_session):
    breed, show, org, _dogs, entries = await _setup(db_session, checkin=True)
    entries[0].attendance_status = AttendanceStatus.absent
    await db_session.commit()
    with pytest.raises(ValueError, match="entry_not_admitted"):
        await _bob(db_session, show, org, breed, entries[0])
