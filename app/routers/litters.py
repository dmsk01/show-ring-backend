"""
Роутер помётов (этап 5).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.litter import LitterStatus
from app.models.user import User
from app.repositories import litter as repo
from app.schemas.litter import (
    LitterCreate,
    LitterPage,
    LitterResponse,
    LitterUpdate,
)
from app.services import litter as svc

router = APIRouter(prefix="/litters", tags=["litters"])


def _is_admin(user: User) -> bool:
    return any(r.role.value == "admin" for r in user.roles)


def _raise_for_error(err: ValueError) -> None:
    code = str(err)
    if code in ("not_found", "kennel_not_found", "father_not_found", "mother_not_found"):
        raise HTTPException(404, code)
    if code == "forbidden":
        raise HTTPException(403, code)
    if code in (
        "father_must_be_male",
        "mother_must_be_female",
        "father_breed_mismatch",
        "mother_breed_mismatch",
    ):
        raise HTTPException(422, code)
    raise HTTPException(400, code)


@router.post(
    "",
    response_model=LitterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Опубликовать помёт",
)
async def create_litter(
    body: LitterCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return await svc.create_litter(
            db,
            requester_id=user.id,
            is_admin=_is_admin(user),
            fields=body.model_dump(),
        )
    except ValueError as e:
        _raise_for_error(e)


@router.get(
    "",
    response_model=LitterPage,
    summary="Список помётов",
    description="Фильтры: питомник, порода, статус. Пагинация.",
)
async def list_litters(
    kennel_id: uuid.UUID | None = Query(None),
    breed_id: uuid.UUID | None = Query(None),
    status_: LitterStatus | None = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    items = await repo.list_litters(
        db,
        kennel_id=kennel_id,
        breed_id=breed_id,
        status=status_,
        page=page,
        per_page=per_page,
    )
    total = await repo.count_litters(
        db, kennel_id=kennel_id, breed_id=breed_id, status=status_
    )
    return LitterPage(
        items=[LitterResponse.model_validate(x) for x in items],
        total=total,
        page=page,
        per_page=per_page,
    )


@router.get(
    "/{litter_id}",
    response_model=LitterResponse,
    summary="Карточка помёта",
)
async def get_litter(litter_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    obj = await repo.get_litter(db, litter_id)
    if obj is None:
        raise HTTPException(404, "Помёт не найден")
    return obj


@router.put(
    "/{litter_id}",
    response_model=LitterResponse,
    summary="Обновить помёт",
)
async def update_litter(
    litter_id: uuid.UUID,
    body: LitterUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return await svc.update_litter(
            db,
            litter_id=litter_id,
            requester_id=user.id,
            is_admin=_is_admin(user),
            fields=body.model_dump(exclude_unset=True),
        )
    except ValueError as e:
        _raise_for_error(e)
