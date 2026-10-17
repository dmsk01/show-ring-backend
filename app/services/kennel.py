"""
Сервис питомников (этап 4).

Бизнес-правила:
- Создавать питомник может только аутентифицированный юзер.
  Роутер передаёт current_user.id как owner_id.
- Редактировать может только владелец (или admin).
- Уникальность kennel_prefix — на уровне БД (UNIQUE). Сервис ловит
  IntegrityError и маппит на ValueError("duplicate_prefix").
"""

from __future__ import annotations

import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import Conflict, Forbidden, NotFound
from app.models.kennel import Kennel
from app.repositories import kennel as repo
from app.policies import Actor


async def create_kennel(
    db: AsyncSession, owner_id: uuid.UUID, **fields
) -> Kennel:
    obj = Kennel(owner_id=owner_id, **fields)
    db.add(obj)
    try:
        await db.commit()
        await db.refresh(obj)
        return obj
    except IntegrityError:
        await db.rollback()
        raise Conflict("duplicate_prefix")


async def update_kennel(
    db: AsyncSession,
    kennel_id: uuid.UUID,
    actor: Actor,
        fields: dict,
) -> Kennel:
    obj = await repo.get_kennel(db, kennel_id)
    if obj is None:
        raise NotFound("not_found")
    # Только владелец или admin может править. Проверяем здесь, а не
    # в роутере, чтобы любой callsite не мог случайно обойти проверку.
    if obj.owner_id != actor.user_id and not actor.is_admin:
        raise Forbidden("forbidden")
    for k, v in fields.items():
        setattr(obj, k, v)
    try:
        await db.commit()
        await db.refresh(obj)
        return obj
    except IntegrityError:
        await db.rollback()
        raise Conflict("duplicate_prefix")


async def delete_kennel(
    db: AsyncSession,
    kennel_id: uuid.UUID,
    actor: Actor,
    ) -> None:
    obj = await repo.get_kennel(db, kennel_id)
    if obj is None:
        raise NotFound("not_found")
    if obj.owner_id != actor.user_id and not actor.is_admin:
        raise Forbidden("forbidden")
    # FK dog.kennel_id SET NULL — собаки останутся в БД без питомника.
    # Это сознательно: мы не теряем исторических данных о собаках.
    await db.delete(obj)
    await db.commit()
