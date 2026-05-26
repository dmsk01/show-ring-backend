"""
Репозиторий результатов и титулов (этап 7).

Здесь — самые сложные SQL-запросы в проекте на данный момент:
- multi-table JOIN: results + entries + dogs + breeds + classes + grades
- агрегаты для каталога результатов по породе/группе
- доступ к dog_titles для проверки квалификации классов
"""

from __future__ import annotations

import uuid
from typing import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dog import Dog
from app.models.reference import Breed, Grade
from app.models.result import DogTitle, ShowResult
from app.models.show import ShowEntry


# ---------------------------------------------------------------------
# ShowResult — CRUD
# ---------------------------------------------------------------------


async def get_result(
    db: AsyncSession, id_: uuid.UUID
) -> ShowResult | None:
    return await db.get(ShowResult, id_)


async def get_result_by_entry(
    db: AsyncSession, show_entry_id: uuid.UUID
) -> ShowResult | None:
    stmt = select(ShowResult).where(ShowResult.show_entry_id == show_entry_id)
    return (await db.execute(stmt)).scalar_one_or_none()


async def create_result(db: AsyncSession, **fields) -> ShowResult:
    obj = ShowResult(**fields)
    db.add(obj)
    await db.flush()
    return obj


async def list_results_for_show(
    db: AsyncSession,
    show_id: uuid.UUID,
    *,
    page: int = 1,
    per_page: int = 200,
) -> Sequence[ShowResult]:
    """
    Все результаты выставки. JOIN через ShowEntry — без него мы не
    знаем, какой результат к какой выставке принадлежит (FK у result
    смотрит на entry, не на show).
    """
    stmt = (
        select(ShowResult)
        .join(ShowEntry, ShowEntry.id == ShowResult.show_entry_id)
        .where(ShowEntry.show_id == show_id)
        # Сначала по номеру каталога — естественный порядок осмотра.
        .order_by(ShowEntry.catalog_number.asc().nullslast())
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    return (await db.execute(stmt)).scalars().all()


async def count_results_for_show(
    db: AsyncSession, show_id: uuid.UUID
) -> int:
    stmt = (
        select(func.count())
        .select_from(ShowResult)
        .join(ShowEntry, ShowEntry.id == ShowResult.show_entry_id)
        .where(ShowEntry.show_id == show_id)
    )
    return int((await db.execute(stmt)).scalar_one())


async def list_results_by_breed(
    db: AsyncSession,
    show_id: uuid.UUID,
    breed_id: uuid.UUID,
) -> Sequence[ShowResult]:
    """
    Результаты в породе. Используется для определения ЛК/ЛС/ЛПП — там
    нужны CW по классам этой породы.
    """
    stmt = (
        select(ShowResult)
        .join(ShowEntry, ShowEntry.id == ShowResult.show_entry_id)
        .join(Dog, Dog.id == ShowEntry.dog_id)
        .where(ShowEntry.show_id == show_id, Dog.breed_id == breed_id)
        .order_by(
            ShowEntry.show_class_id,
            ShowResult.placement.asc().nullslast(),
        )
    )
    return (await db.execute(stmt)).scalars().all()


async def list_results_by_group(
    db: AsyncSession,
    show_id: uuid.UUID,
    breed_group_id: uuid.UUID,
) -> Sequence[ShowResult]:
    """
    Все BOB по породам этой группы. Используется для определения BIG.
    """
    stmt = (
        select(ShowResult)
        .join(ShowEntry, ShowEntry.id == ShowResult.show_entry_id)
        .join(Dog, Dog.id == ShowEntry.dog_id)
        .join(Breed, Breed.id == Dog.breed_id)
        .where(
            ShowEntry.show_id == show_id,
            Breed.breed_group_id == breed_group_id,
            ShowResult.is_best_of_breed.is_(True),
        )
    )
    return (await db.execute(stmt)).scalars().all()


async def list_bob_results_for_show(
    db: AsyncSession, show_id: uuid.UUID
) -> Sequence[ShowResult]:
    """Все BOB выставки — кандидаты на BIS (через BIG)."""
    stmt = (
        select(ShowResult)
        .join(ShowEntry, ShowEntry.id == ShowResult.show_entry_id)
        .where(
            ShowEntry.show_id == show_id,
            ShowResult.is_best_in_group.is_(True),
        )
    )
    return (await db.execute(stmt)).scalars().all()


async def list_results_by_ring(
    db: AsyncSession,
    show_id: uuid.UUID,
    breed_id: uuid.UUID | None,
    show_class_id: uuid.UUID | None,
) -> Sequence[ShowResult]:
    """
    Результаты конкретного ринга (порода + класс) или ринга-секции
    группы пород.
    """
    stmt = (
        select(ShowResult)
        .join(ShowEntry, ShowEntry.id == ShowResult.show_entry_id)
        .join(Dog, Dog.id == ShowEntry.dog_id)
        .where(ShowEntry.show_id == show_id)
    )
    if breed_id is not None:
        stmt = stmt.where(Dog.breed_id == breed_id)
    if show_class_id is not None:
        stmt = stmt.where(ShowEntry.show_class_id == show_class_id)
    stmt = stmt.order_by(ShowResult.placement.asc().nullslast())
    return (await db.execute(stmt)).scalars().all()


# ---------------------------------------------------------------------
# DogTitle
# ---------------------------------------------------------------------


async def list_dog_titles(
    db: AsyncSession, dog_id: uuid.UUID
) -> Sequence[DogTitle]:
    stmt = (
        select(DogTitle)
        .where(DogTitle.dog_id == dog_id)
        .order_by(DogTitle.date_earned.desc())
    )
    return (await db.execute(stmt)).scalars().all()


async def dog_has_title(
    db: AsyncSession, dog_id: uuid.UUID, title_code: str
) -> bool:
    """
    Проверка "есть ли у собаки титул с указанным кодом". Используется
    для квалификации в working/champions классы (этап 6 ставил флаг
    requires_documents, теперь умеем проверять реально).
    """
    from app.models.reference import Title

    stmt = (
        select(func.count())
        .select_from(DogTitle)
        .join(Title, Title.id == DogTitle.title_id)
        .where(DogTitle.dog_id == dog_id, Title.code == title_code)
    )
    return int((await db.execute(stmt)).scalar_one()) > 0


async def create_dog_title(db: AsyncSession, **fields) -> DogTitle:
    obj = DogTitle(**fields)
    db.add(obj)
    await db.flush()
    return obj


# ---------------------------------------------------------------------
# Helpers для проверки контекста результата
# ---------------------------------------------------------------------


async def get_entry_context(
    db: AsyncSession, show_entry_id: uuid.UUID
) -> tuple[ShowEntry, Dog, Breed] | None:
    """
    Загружает запись и связанные с ней собаку и породу. Один запрос,
    три JOIN. Используется в сервисе для валидации входа и определения
    animal_type/breed_group.
    """
    stmt = (
        select(ShowEntry, Dog, Breed)
        .join(Dog, Dog.id == ShowEntry.dog_id)
        .join(Breed, Breed.id == Dog.breed_id)
        .where(ShowEntry.id == show_entry_id)
    )
    row = (await db.execute(stmt)).first()
    if row is None:
        return None
    entry, dog, breed = row
    return entry, dog, breed


async def get_grade(
    db: AsyncSession, grade_id: uuid.UUID
) -> Grade | None:
    return await db.get(Grade, grade_id)
