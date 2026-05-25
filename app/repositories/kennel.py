"""
Репозиторий питомников (этап 4).
"""

from __future__ import annotations

import uuid
from typing import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.kennel import Kennel


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
    page: int = 1,
    per_page: int = 50,
) -> Sequence[Kennel]:
    stmt = (
        _kennel_filter_stmt(city, search)
        .order_by(Kennel.name)
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    return (await db.execute(stmt)).scalars().all()


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
