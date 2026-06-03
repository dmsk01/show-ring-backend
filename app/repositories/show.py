"""
Репозиторий выставок (этап 6).

Здесь же:
- сложные JOIN для каталога (entries + dogs + breeds + classes + owners),
- SELECT FOR UPDATE для предотвращения двойной записи,
- агрегаты (count записей по породам/классам).
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.show import (
    Show,
    ShowBreed,
    ShowEntry,
    ShowJudge,
    ShowRing,
    ShowStatus,
)


# Белый список сортировки (этап 18).
_SHOW_SORT = {"date_start": Show.date_start, "created_at": Show.created_at}


def _show_filter_stmt(
    rank_id: uuid.UUID | None,
    city: str | None,
    date_from: date | None,
    date_to: date | None,
    status: ShowStatus | None,
):
    stmt = select(Show)
    if rank_id is not None:
        stmt = stmt.where(Show.rank_id == rank_id)
    if city:
        stmt = stmt.where(Show.city.ilike(f"%{city}%"))
    if date_from is not None:
        stmt = stmt.where(Show.date_start >= date_from)
    if date_to is not None:
        stmt = stmt.where(Show.date_start <= date_to)
    if status is not None:
        stmt = stmt.where(Show.status == status)
    return stmt


async def get_show(db: AsyncSession, id_: uuid.UUID) -> Show | None:
    return await db.get(Show, id_)


async def get_show_for_update(
    db: AsyncSession, id_: uuid.UUID
) -> Show | None:
    """
    SELECT … FOR UPDATE — блокирует строку до конца транзакции.
    Используется при записи на выставку: между проверкой "не записан ли
    уже" и INSERT блокировка предотвращает гонку, когда два запроса
    проходят проверку одновременно.
    """
    stmt = select(Show).where(Show.id == id_).with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


async def list_shows(
    db: AsyncSession,
    *,
    rank_id: uuid.UUID | None = None,
    city: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    status: ShowStatus | None = None,
    sort_by: str = "date_start",
    order: str = "asc",
    page: int = 1,
    per_page: int = 50,
) -> Sequence[Show]:
    # Ближайшие сверху по умолчанию. По date_start, не created_at —
    # пользователь ищет "что скоро будет", а не "что недавно создали".
    col = _SHOW_SORT.get(sort_by, Show.date_start)
    stmt = (
        _show_filter_stmt(rank_id, city, date_from, date_to, status)
        .order_by(col.asc() if order == "asc" else col.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    return (await db.execute(stmt)).scalars().all()


async def count_shows(
    db: AsyncSession,
    *,
    rank_id: uuid.UUID | None = None,
    city: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    status: ShowStatus | None = None,
) -> int:
    base = _show_filter_stmt(
        rank_id, city, date_from, date_to, status
    ).subquery()
    return int(
        (await db.execute(select(func.count()).select_from(base))).scalar_one()
    )


async def create_show(db: AsyncSession, **fields) -> Show:
    obj = Show(**fields)
    db.add(obj)
    await db.flush()
    return obj


# ---------------------------------------------------------------------
# Allow-list пород
# ---------------------------------------------------------------------


async def list_show_breeds(
    db: AsyncSession, show_id: uuid.UUID
) -> Sequence[ShowBreed]:
    stmt = select(ShowBreed).where(ShowBreed.show_id == show_id)
    return (await db.execute(stmt)).scalars().all()


async def is_breed_allowed(
    db: AsyncSession, show_id: uuid.UUID, breed_id: uuid.UUID
) -> bool:
    """
    Логика "если allow-list пуст — все породы допущены, иначе строгая
    проверка". Один запрос с EXISTS — без избыточной загрузки строк.
    """
    has_any = await db.execute(
        select(func.count())
        .select_from(ShowBreed)
        .where(ShowBreed.show_id == show_id)
    )
    if int(has_any.scalar_one()) == 0:
        return True  # allow-list пуст — всепородная

    match = await db.execute(
        select(func.count())
        .select_from(ShowBreed)
        .where(ShowBreed.show_id == show_id, ShowBreed.breed_id == breed_id)
    )
    return int(match.scalar_one()) > 0


async def add_show_breed(
    db: AsyncSession, show_id: uuid.UUID, breed_id: uuid.UUID
) -> ShowBreed:
    obj = ShowBreed(show_id=show_id, breed_id=breed_id)
    db.add(obj)
    await db.flush()
    return obj


# ---------------------------------------------------------------------
# Судьи
# ---------------------------------------------------------------------


async def list_show_judges(
    db: AsyncSession, show_id: uuid.UUID
) -> Sequence[ShowJudge]:
    stmt = select(ShowJudge).where(ShowJudge.show_id == show_id)
    return (await db.execute(stmt)).scalars().all()


async def get_show_judge(
    db: AsyncSession, judge_record_id: uuid.UUID
) -> ShowJudge | None:
    return await db.get(ShowJudge, judge_record_id)


async def add_show_judge(
    db: AsyncSession,
    show_id: uuid.UUID,
    judge_id: uuid.UUID,
    breed_id: uuid.UUID | None = None,
    breed_group_id: uuid.UUID | None = None,
) -> ShowJudge:
    obj = ShowJudge(
        show_id=show_id,
        judge_id=judge_id,
        breed_id=breed_id,
        breed_group_id=breed_group_id,
    )
    db.add(obj)
    await db.flush()
    return obj


# ---------------------------------------------------------------------
# Ринги
# ---------------------------------------------------------------------


async def list_show_rings(
    db: AsyncSession, show_id: uuid.UUID
) -> Sequence[ShowRing]:
    stmt = (
        select(ShowRing)
        .where(ShowRing.show_id == show_id)
        # Сортировка под "расписание дня": сначала по дате, потом по
        # времени, потом по номеру ринга.
        .order_by(
            ShowRing.ring_date.asc().nullslast(),
            ShowRing.time_start.asc().nullslast(),
            ShowRing.ring_number.asc(),
        )
    )
    return (await db.execute(stmt)).scalars().all()


async def get_show_ring(
    db: AsyncSession, ring_id: uuid.UUID
) -> ShowRing | None:
    return await db.get(ShowRing, ring_id)


async def create_show_ring(db: AsyncSession, **fields) -> ShowRing:
    obj = ShowRing(**fields)
    db.add(obj)
    await db.flush()
    return obj


# ---------------------------------------------------------------------
# Записи участников
# ---------------------------------------------------------------------


async def get_show_entry(
    db: AsyncSession, entry_id: uuid.UUID
) -> ShowEntry | None:
    return await db.get(ShowEntry, entry_id)


async def is_dog_registered(
    db: AsyncSession, show_id: uuid.UUID, dog_id: uuid.UUID
) -> bool:
    stmt = select(func.count()).select_from(ShowEntry).where(
        ShowEntry.show_id == show_id, ShowEntry.dog_id == dog_id
    )
    return int((await db.execute(stmt)).scalar_one()) > 0


async def list_show_entries(
    db: AsyncSession,
    show_id: uuid.UUID,
    *,
    page: int = 1,
    per_page: int = 200,
) -> Sequence[ShowEntry]:
    stmt = (
        select(ShowEntry)
        .where(ShowEntry.show_id == show_id)
        # Сортировка как в каталоге: сначала по catalog_number (NULL в
        # конце — до закрытия регистрации), потом по created_at.
        .order_by(
            ShowEntry.catalog_number.asc().nullslast(),
            ShowEntry.created_at.asc(),
        )
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    return (await db.execute(stmt)).scalars().all()


async def count_show_entries(
    db: AsyncSession, show_id: uuid.UUID
) -> int:
    stmt = select(func.count()).select_from(ShowEntry).where(
        ShowEntry.show_id == show_id
    )
    return int((await db.execute(stmt)).scalar_one())


async def list_user_entries_for_show(
    db: AsyncSession, show_id: uuid.UUID, user_id: uuid.UUID
) -> Sequence[ShowEntry]:
    stmt = select(ShowEntry).where(
        ShowEntry.show_id == show_id,
        ShowEntry.registered_by == user_id,
    )
    return (await db.execute(stmt)).scalars().all()


async def create_show_entry(db: AsyncSession, **fields) -> ShowEntry:
    obj = ShowEntry(**fields)
    db.add(obj)
    await db.flush()
    return obj


async def next_catalog_number(
    db: AsyncSession, show_id: uuid.UUID
) -> int:
    """
    Следующий каталожный номер. На небольшие выставки (< 1000 записей)
    одного MAX+1 хватит. SELECT FOR UPDATE на show гарантирует, что
    нумерация идёт последовательно в рамках одной транзакции.
    """
    stmt = select(func.coalesce(func.max(ShowEntry.catalog_number), 0)).where(
        ShowEntry.show_id == show_id
    )
    # `or 0` для pyright: SQL-уровень coalesce уже не даёт NULL, но
    # scalar_one() аннотирован как Any|None — без fallback type checker
    # ругается на int(None) даже несмотря на гарантию SQL.
    raw = (await db.execute(stmt)).scalar_one() or 0
    return int(raw) + 1


# ---------------------------------------------------------------------
# Show + связанные (для GET /shows/{id} с подробностями)
# ---------------------------------------------------------------------


async def get_show_with_relations(
    db: AsyncSession, show_id: uuid.UUID
) -> Show | None:
    """
    Загружает выставку со списком пород/судей/рингов одним пакетом
    запросов (selectinload — N+1-free). Используется в GET /shows/{id}.
    """
    stmt = (
        select(Show)
        .where(Show.id == show_id)
        .options(
            selectinload(Show.breeds),
            selectinload(Show.judges),
            selectinload(Show.rings),
        )
    )
    return (await db.execute(stmt)).scalar_one_or_none()
