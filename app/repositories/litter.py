"""
Репозиторий помётов (этап 5).

Динамические фильтры на SQLAlchemy Core: kennel/breed/status — ORM-уровень
достаточно, без сырого SQL.
"""

from __future__ import annotations

import uuid
from typing import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.litter import Litter, LitterStatus


def _litter_filter_stmt(
    kennel_id: uuid.UUID | None,
    breed_id: uuid.UUID | None,
    status: LitterStatus | None,
):
    stmt = select(Litter)
    if kennel_id is not None:
        stmt = stmt.where(Litter.kennel_id == kennel_id)
    if breed_id is not None:
        stmt = stmt.where(Litter.breed_id == breed_id)
    if status is not None:
        stmt = stmt.where(Litter.status == status)
    return stmt


async def get_litter(db: AsyncSession, id_: uuid.UUID) -> Litter | None:
    return await db.get(Litter, id_)


async def list_litters(
    db: AsyncSession,
    kennel_id: uuid.UUID | None = None,
    breed_id: uuid.UUID | None = None,
    status: LitterStatus | None = None,
    page: int = 1,
    per_page: int = 50,
) -> Sequence[Litter]:
    stmt = (
        _litter_filter_stmt(kennel_id, breed_id, status)
        # Сортировка: сначала новые помёты (по born_at, потом по created_at).
        # nullslast — помёты без даты рождения (planned) уходят вниз,
        # чтобы доступные не тонули среди планируемых.
        .order_by(Litter.born_at.desc().nullslast(), Litter.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    return (await db.execute(stmt)).scalars().all()


async def count_litters(
    db: AsyncSession,
    kennel_id: uuid.UUID | None = None,
    breed_id: uuid.UUID | None = None,
    status: LitterStatus | None = None,
) -> int:
    base = _litter_filter_stmt(kennel_id, breed_id, status).subquery()
    return int(
        (await db.execute(select(func.count()).select_from(base))).scalar_one()
    )


async def create_litter(db: AsyncSession, **fields) -> Litter:
    obj = Litter(**fields)
    db.add(obj)
    await db.flush()
    return obj
