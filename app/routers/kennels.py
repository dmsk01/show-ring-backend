"""
Роутер питомников (этап 4).
"""

from __future__ import annotations

import uuid
from typing import Literal, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.kennel import Kennel
from app.models.user import User
from app.repositories import kennel as repo
from app.schemas.kennel import (
    KennelCreate,
    KennelResponse,
    KennelUpdate,
)
from app.services import kennel as svc

router = APIRouter(prefix="/kennels", tags=["kennels"])


def _is_admin(user: User) -> bool:
    return any(r.role.value == "admin" for r in user.roles)


def _kennel_response(
    kennel: Kennel, dogs_count: int, litters_count: int
) -> KennelResponse:
    """KennelResponse + агрегаты (is_verified тянется из ORM автоматически)."""
    resp = KennelResponse.model_validate(kennel)
    resp.dogs_count = dogs_count
    resp.litters_count = litters_count
    return resp


def _raise_for_error(err: ValueError) -> NoReturn:
    code = str(err)
    if code == "not_found":
        raise HTTPException(404, code)
    if code == "forbidden":
        raise HTTPException(403, code)
    if code == "duplicate_prefix":
        raise HTTPException(409, code)
    raise HTTPException(400, code)


@router.post(
    "",
    response_model=KennelResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Создать питомник",
)
async def create_kennel(
    body: KennelCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # owner_id берём из текущего юзера — не доверяем клиенту.
    try:
        kennel = await svc.create_kennel(db, owner_id=user.id, **body.model_dump())
    except ValueError as e:
        _raise_for_error(e)
    # Новый питомник — счётчики нулевые.
    return _kennel_response(kennel, 0, 0)


@router.get(
    "",
    response_model=list[KennelResponse],
    summary="Список питомников",
)
async def list_kennels(
    city: str | None = Query(None),
    search: str | None = Query(None, max_length=128),
    sort_by: Literal["name", "created_at"] = Query("name"),
    order: Literal["asc", "desc"] = Query("asc"),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    items = await repo.list_kennels(
        db, city=city, search=search, sort_by=sort_by, order=order,
        page=page, per_page=per_page,
    )
    counts = await repo.counts_by_kennels(db, [k.id for k in items])
    return [
        _kennel_response(k, *counts.get(k.id, (0, 0))) for k in items
    ]


@router.get(
    "/{kennel_id}",
    response_model=KennelResponse,
    summary="Страница питомника",
)
async def get_kennel(kennel_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    obj = await repo.get_kennel(db, kennel_id)
    if obj is None:
        raise HTTPException(404, "Питомник не найден")
    counts = await repo.counts_by_kennels(db, [obj.id])
    return _kennel_response(obj, *counts.get(obj.id, (0, 0)))


@router.put(
    "/{kennel_id}",
    response_model=KennelResponse,
    summary="Обновить питомник",
)
async def update_kennel(
    kennel_id: uuid.UUID,
    body: KennelUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        kennel = await svc.update_kennel(
            db,
            kennel_id=kennel_id,
            requester_id=user.id,
            is_admin=_is_admin(user),
            fields=body.model_dump(exclude_unset=True),
        )
    except ValueError as e:
        _raise_for_error(e)
    counts = await repo.counts_by_kennels(db, [kennel.id])
    return _kennel_response(kennel, *counts.get(kennel.id, (0, 0)))


@router.delete(
    "/{kennel_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Удалить питомник",
)
async def delete_kennel(
    kennel_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        await svc.delete_kennel(
            db,
            kennel_id=kennel_id,
            requester_id=user.id,
            is_admin=_is_admin(user),
        )
    except ValueError as e:
        _raise_for_error(e)
