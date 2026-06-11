"""
Репозиторий справочников (этап 3).

Здесь сидят SQL-запросы к таблицам справочников. Сервис над ним
наслаивает бизнес-логику (валидацию ссылок, разруливание ошибок).

Соглашение:
- get_* — одиночное чтение по PK или unique-ключу.
- list_* — список с фильтрами и пагинацией.
- create/update/delete — CRUD без коммита (коммит делает сервис/роутер,
  чтобы можно было собрать несколько операций в одну транзакцию).
- count_* — count(*) с теми же фильтрами, чтобы вернуть total в пагинации.

Пагинация limit/offset (а не cursor): на справочниках выборки маленькие
(сотни-тысячи строк) и стабильные, поэтому смысла в курсорах нет.
"""

from __future__ import annotations

import uuid
from typing import Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.reference import (
    AnimalType,
    Breed,
    BreedGroup,
    Grade,
    ShowClass,
    ShowRank,
    Title,
)


# ---------------------------------------------------------------------
# AnimalType
# ---------------------------------------------------------------------


async def get_animal_type(db: AsyncSession, id_: uuid.UUID) -> AnimalType | None:
    return await db.get(AnimalType, id_)


async def get_animal_type_by_code(db: AsyncSession, code: str) -> AnimalType | None:
    stmt = select(AnimalType).where(AnimalType.code == code)
    return (await db.execute(stmt)).scalar_one_or_none()


async def list_animal_types(db: AsyncSession) -> Sequence[AnimalType]:
    stmt = select(AnimalType).order_by(AnimalType.code)
    return (await db.execute(stmt)).scalars().all()


async def create_animal_type(db: AsyncSession, **fields) -> AnimalType:
    obj = AnimalType(**fields)
    db.add(obj)
    await db.flush()
    return obj


async def delete_animal_type(db: AsyncSession, id_: uuid.UUID) -> bool:
    obj = await db.get(AnimalType, id_)
    if obj is None:
        return False
    await db.delete(obj)
    await db.flush()
    return True


# ---------------------------------------------------------------------
# BreedGroup
# ---------------------------------------------------------------------


async def get_breed_group(db: AsyncSession, id_: uuid.UUID) -> BreedGroup | None:
    return await db.get(BreedGroup, id_)


async def list_breed_groups(
    db: AsyncSession, animal_type_id: uuid.UUID | None = None
) -> Sequence[BreedGroup]:
    stmt = select(BreedGroup).order_by(BreedGroup.number)
    if animal_type_id is not None:
        stmt = stmt.where(BreedGroup.animal_type_id == animal_type_id)
    return (await db.execute(stmt)).scalars().all()


async def create_breed_group(db: AsyncSession, **fields) -> BreedGroup:
    obj = BreedGroup(**fields)
    db.add(obj)
    await db.flush()
    return obj


async def delete_breed_group(db: AsyncSession, id_: uuid.UUID) -> bool:
    obj = await db.get(BreedGroup, id_)
    if obj is None:
        return False
    await db.delete(obj)
    await db.flush()
    return True


# ---------------------------------------------------------------------
# Breed
# ---------------------------------------------------------------------


def _breed_filter_stmt(
    animal_type_id: uuid.UUID | None,
    breed_group_id: uuid.UUID | None,
    search: str | None,
):
    """
    Собирает WHERE-условия для list/count, чтобы фильтры применялись
    в одном месте (DRY: count и list используют один и тот же фильтр).
    """
    stmt = select(Breed)
    if animal_type_id is not None:
        stmt = stmt.where(Breed.animal_type_id == animal_type_id)
    if breed_group_id is not None:
        stmt = stmt.where(Breed.breed_group_id == breed_group_id)
    if search:
        # ILIKE — регистронезависимый поиск по подстроке. На сотнях
        # пород производительность не критична; если станет узким
        # местом — добавим pg_trgm + GIN-индекс на этапе оптимизации.
        # Ищем и по русскому, и по английскому имени независимо от
        # локали запроса — пользователь может набрать любое из них.
        pattern = f"%{search}%"
        stmt = stmt.where(
            or_(Breed.name.ilike(pattern), Breed.name_en.ilike(pattern))
        )
    return stmt


async def list_breeds(
    db: AsyncSession,
    animal_type_id: uuid.UUID | None = None,
    breed_group_id: uuid.UUID | None = None,
    search: str | None = None,
    page: int = 1,
    per_page: int = 50,
    locale: str = "ru",
) -> Sequence[Breed]:
    # Для en сортируем по отображаемому имени — переводу с фолбэком на
    # русское (coalesce), чтобы алфавитный порядок соответствовал тому,
    # что реально видит пользователь.
    order_col = (
        func.coalesce(Breed.name_en, Breed.name) if locale == "en" else Breed.name
    )
    stmt = (
        _breed_filter_stmt(animal_type_id, breed_group_id, search)
        .order_by(order_col)
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    return (await db.execute(stmt)).scalars().all()


async def count_breeds(
    db: AsyncSession,
    animal_type_id: uuid.UUID | None = None,
    breed_group_id: uuid.UUID | None = None,
    search: str | None = None,
) -> int:
    # subquery + count(*) — самый прямолинейный путь получить total
    # под те же фильтры, что и list.
    base = _breed_filter_stmt(animal_type_id, breed_group_id, search).subquery()
    stmt = select(func.count()).select_from(base)
    return int((await db.execute(stmt)).scalar_one())


async def get_breed(db: AsyncSession, id_: uuid.UUID) -> Breed | None:
    return await db.get(Breed, id_)


async def create_breed(db: AsyncSession, **fields) -> Breed:
    obj = Breed(**fields)
    db.add(obj)
    await db.flush()
    return obj


async def delete_breed(db: AsyncSession, id_: uuid.UUID) -> bool:
    obj = await db.get(Breed, id_)
    if obj is None:
        return False
    await db.delete(obj)
    await db.flush()
    return True


# ---------------------------------------------------------------------
# ShowClass
# ---------------------------------------------------------------------


async def get_show_class(db: AsyncSession, id_: uuid.UUID) -> ShowClass | None:
    return await db.get(ShowClass, id_)


async def list_show_classes(
    db: AsyncSession, animal_type_id: uuid.UUID | None = None
) -> Sequence[ShowClass]:
    stmt = select(ShowClass).order_by(ShowClass.age_from_months)
    if animal_type_id is not None:
        stmt = stmt.where(ShowClass.animal_type_id == animal_type_id)
    return (await db.execute(stmt)).scalars().all()


async def create_show_class(db: AsyncSession, **fields) -> ShowClass:
    obj = ShowClass(**fields)
    db.add(obj)
    await db.flush()
    return obj


async def delete_show_class(db: AsyncSession, id_: uuid.UUID) -> bool:
    obj = await db.get(ShowClass, id_)
    if obj is None:
        return False
    await db.delete(obj)
    await db.flush()
    return True


# ---------------------------------------------------------------------
# ShowRank
# ---------------------------------------------------------------------


async def get_show_rank(db: AsyncSession, id_: uuid.UUID) -> ShowRank | None:
    return await db.get(ShowRank, id_)


async def list_show_ranks(db: AsyncSession) -> Sequence[ShowRank]:
    stmt = select(ShowRank).order_by(ShowRank.code)
    return (await db.execute(stmt)).scalars().all()


async def create_show_rank(db: AsyncSession, **fields) -> ShowRank:
    obj = ShowRank(**fields)
    db.add(obj)
    await db.flush()
    return obj


async def delete_show_rank(db: AsyncSession, id_: uuid.UUID) -> bool:
    obj = await db.get(ShowRank, id_)
    if obj is None:
        return False
    await db.delete(obj)
    await db.flush()
    return True


# ---------------------------------------------------------------------
# Title
# ---------------------------------------------------------------------


async def get_title(db: AsyncSession, id_: uuid.UUID) -> Title | None:
    return await db.get(Title, id_)


async def list_titles(
    db: AsyncSession, animal_type_id: uuid.UUID | None = None
) -> Sequence[Title]:
    stmt = select(Title).order_by(Title.code)
    if animal_type_id is not None:
        stmt = stmt.where(Title.animal_type_id == animal_type_id)
    return (await db.execute(stmt)).scalars().all()


async def create_title(db: AsyncSession, **fields) -> Title:
    obj = Title(**fields)
    db.add(obj)
    await db.flush()
    return obj


async def delete_title(db: AsyncSession, id_: uuid.UUID) -> bool:
    obj = await db.get(Title, id_)
    if obj is None:
        return False
    await db.delete(obj)
    await db.flush()
    return True


# ---------------------------------------------------------------------
# Grade
# ---------------------------------------------------------------------


async def get_grade(db: AsyncSession, id_: uuid.UUID) -> Grade | None:
    return await db.get(Grade, id_)


async def list_grades(
    db: AsyncSession, animal_type_id: uuid.UUID | None = None
) -> Sequence[Grade]:
    stmt = select(Grade).order_by(Grade.code)
    if animal_type_id is not None:
        stmt = stmt.where(Grade.animal_type_id == animal_type_id)
    return (await db.execute(stmt)).scalars().all()


async def create_grade(db: AsyncSession, **fields) -> Grade:
    obj = Grade(**fields)
    db.add(obj)
    await db.flush()
    return obj


async def delete_grade(db: AsyncSession, id_: uuid.UUID) -> bool:
    obj = await db.get(Grade, id_)
    if obj is None:
        return False
    await db.delete(obj)
    await db.flush()
    return True


# ---------------------------------------------------------------------
# Проверки ссылок (для безопасного удаления)
# ---------------------------------------------------------------------


async def breed_group_has_breeds(db: AsyncSession, group_id: uuid.UUID) -> bool:
    """
    True, если на группу есть хотя бы одна порода.
    EXISTS-подзапрос быстрее COUNT(*) — БД останавливает скан после
    первой найденной записи.
    """
    stmt = select(func.count()).select_from(
        select(Breed.id).where(Breed.breed_group_id == group_id).limit(1).subquery()
    )
    return int((await db.execute(stmt)).scalar_one()) > 0


async def animal_type_has_dependents(db: AsyncSession, id_: uuid.UUID) -> bool:
    """
    True, если на animal_type ссылается хоть один справочник.
    Реализация общая — на каждую дочернюю таблицу EXISTS.
    """
    for model in (Breed, BreedGroup, ShowClass, Title, Grade):
        stmt = select(
            select(model.id).where(model.animal_type_id == id_).exists()
        )
        if (await db.execute(stmt)).scalar():
            return True
    return False
