r"""
Seed-скрипт тестовой выставки для проверки генерации официальных документов
(этап 8): каталог, дипломы, ринговые ведомости.

Запуск:
    .\venv\Scripts\python.exe -m scripts.seed_test_show

Что делает (идемпотентно по натуральным ключам):
1. Гарантирует справочники (зовёт scripts.seed_references.seed).
2. Создаёт организатора (+роль organizer), двух судей, заводчиков —
   всем заводит UserProfile (ФИО/страна), иначе в документах будут email.
3. Создаёт питомники (для граф «владелец»/«заводчик»).
4. Создаёт собак (с породой, заводчиком, родословной отец×мать).
5. Создаёт выставку (статус completed), ринги, назначения судей,
   записи с номерами каталога и результаты с оценками/титулами.
6. Печатает show_id, пример entry_id и логин организатора.

Если выставка с тем же именем у организатора уже есть — повторно граф
записей не создаёт, только печатает её id (создание «один раз»).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_factory, engine
# Регистрируем все модели в Base.metadata (ленивые FK).
from app.models import (  # noqa: F401
    ad, audit, classified, dog, file, kennel, litter,
    notification, outbox, reference, result, show, support, task,
)
from app.models.dog import Dog, SexEnum
from app.models.kennel import Kennel
from app.models.reference import Breed, BreedGroup, Grade, ShowClass, ShowRank
from app.models.result import ShowResult
from app.models.show import Show, ShowEntry, ShowJudge, ShowRing, ShowStatus
from app.models.user import RoleEnum, User, UserProfile, UserRole
from app.utils.security import hash_password
from scripts.seed_references import seed as seed_references

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("seed-show")

SHOW_NAME = "Тестовая национальная выставка (сид)"
ORG_EMAIL = "organizer@test.local"
ORG_PASSWORD = "TestPass123!"


async def _get_or_create(db, model, lookup: dict, create: dict):
    stmt = select(model)
    for k, v in lookup.items():
        stmt = stmt.where(getattr(model, k) == v)
    obj = (await db.execute(stmt)).scalar_one_or_none()
    if obj is not None:
        return obj, False
    obj = model(**create)
    db.add(obj)
    await db.flush()
    return obj, True


async def _user_with_profile(
    db, email, *, last, first, patr=None, country="Россия", role=None
) -> User:
    user, created = await _get_or_create(
        db, User, {"email": email},
        {
            "email": email,
            "hashed_password": hash_password(ORG_PASSWORD),
            "is_active": True,
            "is_email_verified": True,
        },
    )
    await _get_or_create(
        db, UserProfile, {"user_id": user.id},
        {
            "user_id": user.id, "last_name": last, "first_name": first,
            "patronymic": patr, "country": country,
        },
    )
    if role is not None:
        await _get_or_create(
            db, UserRole, {"user_id": user.id, "role": role},
            {"user_id": user.id, "role": role, "granted_by": user.id},
        )
    return user


async def seed(db: AsyncSession) -> None:
    # 0. Справочники.
    await seed_references(db)

    # 1. Люди.
    organizer = await _user_with_profile(
        db, ORG_EMAIL, last="Ширкина", first="Маргарита", patr="Анатольевна",
        role=RoleEnum.organizer,
    )
    judge1 = await _user_with_profile(
        db, "judge1@test.local", last="Мордвинова", first="Татьяна",
        patr="Александровна", role=RoleEnum.judge,
    )
    judge2 = await _user_with_profile(
        db, "judge2@test.local", last="Гришина", first="Евгения",
        patr="Евгеньевна", role=RoleEnum.judge,
    )
    breeder_u = await _user_with_profile(
        db, "breeder@test.local", last="Сидорова", first="Анна",
        role=RoleEnum.breeder,
    )
    owner_u = await _user_with_profile(
        db, "owner@test.local", last="Петров", first="Пётр", patr="Петрович",
        role=RoleEnum.buyer,
    )

    # Выставка существует? Тогда только печатаем id (создание один раз).
    existing = (
        await db.execute(
            select(Show).where(
                Show.organizer_id == organizer.id, Show.name == SHOW_NAME
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        await db.commit()
        entry = (
            await db.execute(
                select(ShowEntry).where(ShowEntry.show_id == existing.id).limit(1)
            )
        ).scalar_one_or_none()
        _print_summary(existing.id, entry.id if entry else None)
        return

    # 2. Питомники (для граф владелец/заводчик).
    breeder_kennel, _ = await _get_or_create(
        db, Kennel, {"name": "Питомник Сидоровой"},
        {"owner_id": breeder_u.id, "name": "Питомник Сидоровой",
         "kennel_prefix": "От Сидоровой", "city": "Москва"},
    )
    owner_kennel, _ = await _get_or_create(
        db, Kennel, {"name": "Питомник Петрова"},
        {"owner_id": owner_u.id, "name": "Питомник Петрова",
         "kennel_prefix": "От Петрова", "city": "Одинцово"},
    )

    # 3. Берём 3 породы из 2 групп FCI.
    groups = (
        await db.execute(select(BreedGroup).order_by(BreedGroup.number).limit(2))
    ).scalars().all()
    chosen: list[Breed] = []
    for grp in groups:
        br = (
            await db.execute(
                select(Breed).where(Breed.breed_group_id == grp.id)
                .order_by(Breed.name).limit(2 if grp is groups[0] else 1)
            )
        ).scalars().all()
        chosen.extend(br)
    if len(chosen) < 3:
        raise SystemExit("Мало пород в справочнике — запусти seed_references")
    breed_a, breed_b, breed_c = chosen[0], chosen[1], chosen[2]

    cls_open = (
        await db.execute(select(ShowClass).where(ShowClass.code == "open"))
    ).scalar_one()
    cls_junior = (
        await db.execute(select(ShowClass).where(ShowClass.code == "junior"))
    ).scalar_one()
    rank = (
        await db.execute(select(ShowRank).where(ShowRank.code == "cac-chf"))
    ).scalar_one()
    grade_exc = (
        await db.execute(select(Grade).where(Grade.code == "excellent"))
    ).scalar_one()

    # 4. Родители (для родословной отец×мать) — отдельные собаки, не в записях.
    def mk_dog(name, breed, sex, dob, color, rkf, **extra) -> Dog:
        d = Dog(
            breed_id=breed.id, name=name, sex=sex, date_of_birth=dob,
            color=color, rkf_number=rkf, **extra,
        )
        db.add(d)
        return d

    sire = mk_dog("ГРАНД ОТ КАХОВКИ", breed_a, SexEnum.male,
                  date(2018, 5, 1), "чёрный", "RKF-SIRE-1")
    dam = mk_dog("ЛЕДИ ОТ КАХОВКИ", breed_a, SexEnum.female,
                 date(2019, 6, 1), "чёрный", "RKF-DAM-1")
    await db.flush()

    # 5. Выставочные собаки.
    dogs_spec = [
        ("РЕКС ОТ СИДОРОВОЙ", breed_a, SexEnum.male, date(2023, 2, 2),
         "чёрно-подпалый", "RKF-1001", cls_open),
        ("БЕЛЛА ОТ СИДОРОВОЙ", breed_a, SexEnum.female, date(2024, 9, 10),
         "рыжий", "RKF-1002", cls_junior),
        ("ЛЮНА ОТ ПЕТРОВА", breed_b, SexEnum.female, date(2022, 3, 15),
         "тигровый", "RKF-1003", cls_open),
        ("МАКС ОТ ПЕТРОВА", breed_c, SexEnum.male, date(2021, 11, 20),
         "белый", "RKF-1004", cls_open),
    ]
    show = Show(
        organizer_id=organizer.id, rank_id=rank.id, name=SHOW_NAME,
        date_start=date(2025, 11, 22), city="Москва", country="Россия",
        venue="СФЕРА", status=ShowStatus.completed,
    )
    db.add(show)
    await db.flush()

    # Ринги + назначения судей по породам.
    judges_cycle = [judge1, judge2, judge1]
    for i, breed in enumerate([breed_a, breed_b, breed_c]):
        jdg = judges_cycle[i]
        db.add(ShowRing(
            show_id=show.id, ring_number=1, breed_id=breed.id,
            judge_id=jdg.id, ring_date=show.date_start,
        ))
        db.add(ShowJudge(show_id=show.id, judge_id=jdg.id, breed_id=breed.id))

    # Записи + результаты.
    first_entry_id = None
    for i, (name, breed, sex, dob, color, rkf, cls) in enumerate(dogs_spec, start=1):
        d = Dog(
            breed_id=breed.id, name=name, sex=sex, date_of_birth=dob,
            color=color, rkf_number=rkf,
            tattoo=f"T{i:03d}", microchip=f"6430941001234{i:02d}",
            breeder_kennel_id=breeder_kennel.id, kennel_id=owner_kennel.id,
            father_id=sire.id, mother_id=dam.id,
        )
        db.add(d)
        await db.flush()
        entry = ShowEntry(
            show_id=show.id, dog_id=d.id, show_class_id=cls.id,
            catalog_number=i, registered_by=organizer.id,
        )
        db.add(entry)
        await db.flush()
        if first_entry_id is None:
            first_entry_id = entry.id
        # Результат: отлично + CW/CAC для первого.
        titles = [{"code": "CW", "name": "CW"}, {"code": "CAC", "name": "CAC"}] \
            if i == 1 else [{"code": "CW", "name": "CW"}]
        db.add(ShowResult(
            show_entry_id=entry.id, judge_id=judges_cycle[0].id,
            grade_id=grade_exc.id, placement=1, is_class_winner=True,
            titles_cache=titles,
        ))

    await db.commit()
    _print_summary(show.id, first_entry_id)


def _print_summary(show_id, entry_id) -> None:
    print("\n" + "=" * 60)
    print("СИД ГОТОВ")
    print(f"  show_id  = {show_id}")
    print(f"  entry_id = {entry_id}")
    print(f"  организатор: {ORG_EMAIL} / {ORG_PASSWORD}")
    print("=" * 60)


async def main() -> None:
    try:
        async with async_session_factory() as db:
            await seed(db)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
