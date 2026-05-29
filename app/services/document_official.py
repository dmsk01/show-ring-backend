# app/services/document_official.py
"""
Билдеры контекста для официальных документов РКФ (docxtpl).

Разделение: чистые `_shape_*` собирают словарь из простых значений
(тестируются без БД); `build_*_context` грузят ORM и зовут шейпер.

Имена людей резолвятся через app.utils.names (profile должен быть
подгружен заранее).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dog import Dog
from app.models.kennel import Kennel
from app.models.reference import Breed, BreedGroup, Grade, ShowClass, ShowRank
from app.models.result import ShowResult
from app.models.show import Show, ShowEntry, ShowJudge, ShowRing
from app.models.user import User
from app.utils.names import full_name, judge_display


def _fmt_date(value: date | None) -> str:
    return value.strftime("%d.%m.%Y") if value else ""


def _s(value: object | None) -> str:
    return str(value) if value not in (None, "") else ""


# ---------------------------------------------------------------------
# Резолвер имён с профилем (async)
# ---------------------------------------------------------------------


async def _load_user_with_profile(
    db: AsyncSession, user_id: uuid.UUID | None
) -> User | None:
    if user_id is None:
        return None
    user = await db.get(User, user_id)
    if user is not None:
        # AsyncAttrs (Base) — ленивую связь profile грузим явно.
        await user.awaitable_attrs.profile
    return user


# ---------------------------------------------------------------------
# Диплом
# ---------------------------------------------------------------------


@dataclass
class DiplomaInput:
    show_name: str
    judge: str | None
    breed: str
    sex: str  # "male" | "female"
    class_name: str
    grade: str | None
    title: str | None
    placement: int | None
    dog_name: str
    tattoo: str | None
    microchip: str | None
    date_of_birth: date | None
    owner: str | None
    kennel: str | None
    breeder: str | None
    pedigree: str | None


def _shape_diploma_context(data: DiplomaInput) -> dict:
    return {
        "show_name": _s(data.show_name),
        "judge": _s(data.judge),
        "breed": _s(data.breed),
        "sex_male": data.sex == "male",
        "sex_female": data.sex == "female",
        "class_name": _s(data.class_name),
        "grade": _s(data.grade),
        "title": _s(data.title),
        "place": _s(data.placement),
        "dog_name": _s(data.dog_name),
        "tattoo": _s(data.tattoo),
        "microchip": _s(data.microchip),
        "dob": _fmt_date(data.date_of_birth),
        "owner": _s(data.owner),
        "kennel": _s(data.kennel),
        "breeder": _s(data.breeder),
        "pedigree": _s(data.pedigree),
    }


async def _resolve_breeder(
    db: AsyncSession, dog: Dog
) -> tuple[str, str]:
    """Возвращает (breeder_name, breeder_kennel_prefix)."""
    if dog.breeder_kennel_id is not None:
        kennel = await db.get(Kennel, dog.breeder_kennel_id)
        if kennel is not None:
            owner = await _load_user_with_profile(db, kennel.owner_id)
            return full_name(owner), _s(kennel.kennel_prefix or kennel.name)
    return _s(dog.breeder_name), ""


async def _resolve_owner(db: AsyncSession, dog: Dog) -> str:
    if dog.kennel_id is not None:
        kennel = await db.get(Kennel, dog.kennel_id)
        if kennel is not None:
            owner = await _load_user_with_profile(db, kennel.owner_id)
            return full_name(owner)
    return ""


async def build_diploma_context(
    db: AsyncSession, entry_id: uuid.UUID
) -> dict:
    entry = await db.get(ShowEntry, entry_id)
    if entry is None:
        raise ValueError("entry_not_found")
    show = await db.get(Show, entry.show_id)
    if show is None:
        raise ValueError("not_found")
    dog = await db.get(Dog, entry.dog_id)
    if dog is None:
        raise ValueError("dog_not_found")
    breed = await db.get(Breed, dog.breed_id)
    cls = await db.get(ShowClass, entry.show_class_id)

    result = (
        await db.execute(
            select(ShowResult).where(ShowResult.show_entry_id == entry_id)
        )
    ).scalar_one_or_none()

    grade_name = None
    judge = None
    title = None
    if result is not None:
        if result.grade_id is not None:
            grade = await db.get(Grade, result.grade_id)
            grade_name = grade.name if grade else None
        judge_user = await _load_user_with_profile(db, result.judge_id)
        judge = judge_display(judge_user) if judge_user else None
        titles = [
            t.get("name", t.get("code", ""))
            for t in (result.titles_cache or [])
        ]
        title = ", ".join(t for t in titles if t) or None

    owner = await _resolve_owner(db, dog)
    breeder, kennel_prefix = await _resolve_breeder(db, dog)

    return _shape_diploma_context(
        DiplomaInput(
            show_name=show.name,
            judge=judge,
            breed=breed.name if breed else "",
            sex=dog.sex.value,
            class_name=cls.name if cls else "",
            grade=grade_name,
            title=title,
            placement=result.placement if result else None,
            dog_name=dog.name,
            tattoo=dog.tattoo,
            microchip=dog.microchip,
            date_of_birth=dog.date_of_birth,
            owner=owner,
            kennel=kennel_prefix,
            breeder=breeder,
            pedigree=dog.rkf_number,
        )
    )
