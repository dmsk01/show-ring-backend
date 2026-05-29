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


# ---------------------------------------------------------------------
# Ринговая ведомость
# ---------------------------------------------------------------------


_SEX_RU = {"male": "кобели", "female": "суки"}


@dataclass
class RingRowInput:
    catalog_number: int | None
    dog_name: str
    date_of_birth: str  # уже форматированная дата
    color: str | None
    pedigree: str | None
    tattoo: str | None
    microchip: str | None
    breeder: str | None
    owner: str | None


@dataclass
class RingSheetInput:
    city: str | None
    date: str
    judge: str | None
    breed: str
    ring_number: int | None
    class_name: str
    sex: str
    rows: list[RingRowInput]


def _shape_ring_row(r: RingRowInput) -> dict:
    name_dob_color = ", ".join(
        x for x in [_s(r.dog_name), _s(r.date_of_birth), _s(r.color)] if x
    )
    marks = ", ".join(
        x for x in [_s(r.tattoo), _s(r.microchip)] if x
    )
    pedigree_marks = " / ".join(
        x for x in [_s(r.pedigree), marks] if x
    )
    breeder_owner = " / ".join(
        x for x in [_s(r.breeder), _s(r.owner)] if x
    )
    return {
        "catalog_number": _s(r.catalog_number),
        "name_dob_color": name_dob_color,
        "pedigree_marks": pedigree_marks,
        "breeder_owner": breeder_owner,
        # Пустые колонки — судья заполняет от руки.
        "grade": "",
        "titles": "",
        "place": "",
        "litter": "",
        "total": "",
    }


def _shape_ring_sheet(data: RingSheetInput) -> dict:
    return {
        "city": _s(data.city),
        "date": _s(data.date),
        "judge": _s(data.judge),
        "breed": _s(data.breed),
        "ring_number": _s(data.ring_number),
        "class_name": _s(data.class_name),
        "sex": _SEX_RU.get(data.sex, _s(data.sex)),
        "rows": [_shape_ring_row(r) for r in data.rows],
    }


async def build_ring_sheets_context(
    db: AsyncSession,
    show_id: uuid.UUID,
    ring_id: uuid.UUID | None = None,
) -> dict:
    """
    Контекст для одного файла со всеми ведомостями выставки (или одного
    ринга, если задан ring_id). Группировка: ринг → порода/класс → пол.

    Ведомость в образце сделана на (ринг + порода + класс + пол). Здесь
    собираем по рингам из ShowRing, а внутри ринга — по записям нужной
    породы/класса, разбивая по полу.
    """
    show = await db.get(Show, show_id)
    if show is None:
        raise ValueError("not_found")

    rings_stmt = select(ShowRing).where(ShowRing.show_id == show_id)
    if ring_id is not None:
        rings_stmt = rings_stmt.where(ShowRing.id == ring_id)
    rings_stmt = rings_stmt.order_by(ShowRing.ring_number.asc())
    rings = (await db.execute(rings_stmt)).scalars().all()

    sheets: list[dict] = []
    for ring in rings:
        if ring.breed_id is None:
            continue  # ведомость строится по конкретной породе ринга
        breed = await db.get(Breed, ring.breed_id)
        judge_user = await _load_user_with_profile(db, ring.judge_id)
        judge = judge_display(judge_user) if judge_user else None

        # Записи этой породы (через собак) в нужном классе ринга.
        entries = (
            await db.execute(
                select(ShowEntry)
                .where(ShowEntry.show_id == show_id)
                .order_by(ShowEntry.catalog_number.asc().nullslast())
            )
        ).scalars().all()

        cls = (
            await db.get(ShowClass, ring.show_class_id)
            if ring.show_class_id
            else None
        )

        # Разбиваем по полу.
        rows_by_sex: dict[str, list[RingRowInput]] = {"male": [], "female": []}
        for e in entries:
            dog = await db.get(Dog, e.dog_id)
            if dog is None or dog.breed_id != ring.breed_id:
                continue
            if ring.show_class_id and e.show_class_id != ring.show_class_id:
                continue
            breeder, _prefix = await _resolve_breeder(db, dog)
            owner = await _resolve_owner(db, dog)
            rows_by_sex[dog.sex.value].append(
                RingRowInput(
                    catalog_number=e.catalog_number,
                    dog_name=dog.name,
                    date_of_birth=_fmt_date(dog.date_of_birth),
                    color=dog.color,
                    pedigree=dog.rkf_number,
                    tattoo=dog.tattoo,
                    microchip=dog.microchip,
                    breeder=breeder,
                    owner=owner,
                )
            )

        ring_date = _fmt_date(ring.ring_date) or _fmt_date(show.date_start)
        for sex, rows in rows_by_sex.items():
            if not rows:
                continue
            sheets.append(
                _shape_ring_sheet(
                    RingSheetInput(
                        city=show.city,
                        date=ring_date,
                        judge=judge,
                        breed=breed.name if breed else "",
                        ring_number=ring.ring_number,
                        class_name=cls.name if cls else "",
                        sex=sex,
                        rows=rows,
                    )
                )
            )

    return {"sheets": sheets}
