"""
Репозиторий питомников (этап 4).
"""

from __future__ import annotations

import uuid
from typing import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dog import Dog
from app.models.kennel import Kennel
from app.models.litter import Litter

# Белый список сортировки (этап 18).
_KENNEL_SORT = {"name": Kennel.name, "created_at": Kennel.created_at}


def _kennel_filter_stmt(city: str | None, search: str | None):
    stmt = select(Kennel)
    if city:
        # Точное совпадение по городу — города у нас в виде справочника
        # в будущем будут нормализованы. Пока ILIKE по подстроке.
        stmt = stmt.where(Kennel.city.ilike(f"%{city}%"))
    if search:
        stmt = stmt.where(Kennel.name.ilike(f"%{search}%"))
    return stmt


async def get_kennel(db: AsyncSession, id_: uuid.UUID) -> Kennel | None:
    return await db.get(Kennel, id_)


async def list_kennels(
    db: AsyncSession,
    city: str | None = None,
    search: str | None = None,
    sort_by: str = "name",
    order: str = "asc",
    page: int = 1,
    per_page: int = 50,
) -> Sequence[Kennel]:
    col = _KENNEL_SORT.get(sort_by, Kennel.name)
    stmt = (
        _kennel_filter_stmt(city, search)
        .order_by(col.asc() if order == "asc" else col.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    return (await db.execute(stmt)).scalars().all()


async def counts_by_kennels(
    db: AsyncSession, kennel_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, tuple[int, int]]:
    """{kennel_id: (dogs_count, litters_count)} двумя GROUP BY — анти-N+1."""
    ids = list(kennel_ids)
    if not ids:
        return {}
    dogs_rows = (
        await db.execute(
            select(Dog.kennel_id, func.count())
            .where(Dog.kennel_id.in_(ids))
            .group_by(Dog.kennel_id)
        )
    ).all()
    dogs_map = {kid: int(c) for kid, c in dogs_rows}
    lit_rows = (
        await db.execute(
            select(Litter.kennel_id, func.count())
            .where(Litter.kennel_id.in_(ids))
            .group_by(Litter.kennel_id)
        )
    ).all()
    lit_map = {kid: int(c) for kid, c in lit_rows}
    return {kid: (dogs_map.get(kid, 0), lit_map.get(kid, 0)) for kid in ids}


async def count_kennels(
    db: AsyncSession, city: str | None = None, search: str | None = None
) -> int:
    base = _kennel_filter_stmt(city, search).subquery()
    return int((await db.execute(select(func.count()).select_from(base))).scalar_one())


async def create_kennel(db: AsyncSession, **fields) -> Kennel:
    obj = Kennel(**fields)
    db.add(obj)
    await db.flush()
    return obj


async def list_kennels_by_owner(
    db: AsyncSession, owner_id: uuid.UUID
) -> Sequence[Kennel]:
    stmt = select(Kennel).where(Kennel.owner_id == owner_id).order_by(Kennel.name)
    return (await db.execute(stmt)).scalars().all()
