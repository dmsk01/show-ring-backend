"""
Роутер собак (этап 4).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.dog import SexEnum
from app.models.user import User
from app.repositories import dog as repo
from app.schemas.dog import (
    DogCreate,
    DogPage,
    DogResponse,
    DogUpdate,
    PedigreeNode,
)
from app.services import dog as svc

router = APIRouter(prefix="/dogs", tags=["dogs"])


def _is_admin(user: User) -> bool:
    return any(r.role.value == "admin" for r in user.roles)


def _raise_for_error(err: ValueError) -> None:
    code = str(err)
    if code.endswith("not_found"):
        raise HTTPException(404, code)
    if code == "forbidden":
        raise HTTPException(403, code)
    if code in ("duplicate_unique_field",):
        raise HTTPException(409, code)
    if code in (
        "father_must_be_male",
        "mother_must_be_female",
        "self_parent_forbidden",
    ):
        raise HTTPException(422, code)
    raise HTTPException(400, code)


@router.post(
    "",
    response_model=DogResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Добавить собаку",
)
async def create_dog(
    body: DogCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return await svc.create_dog(
            db,
            requester_id=user.id,
            is_admin=_is_admin(user),
            fields=body.model_dump(),
        )
    except ValueError as e:
        _raise_for_error(e)


@router.get(
    "",
    response_model=DogPage,
    summary="Поиск собак",
    description="Фильтры: порода, питомник, пол; поиск по имени; пагинация.",
)
async def list_dogs(
    breed_id: uuid.UUID | None = Query(None),
    kennel_id: uuid.UUID | None = Query(None),
    sex: SexEnum | None = Query(None),
    search: str | None = Query(None, max_length=128),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    items = await repo.list_dogs(
        db,
        breed_id=breed_id,
        kennel_id=kennel_id,
        sex=sex,
        search=search,
        page=page,
        per_page=per_page,
    )
    total = await repo.count_dogs(
        db, breed_id=breed_id, kennel_id=kennel_id, sex=sex, search=search
    )
    return DogPage(
        items=[DogResponse.model_validate(x) for x in items],
        total=total,
        page=page,
        per_page=per_page,
    )


@router.get(
    "/{dog_id}",
    response_model=DogResponse,
    summary="Карточка собаки",
)
async def get_dog(dog_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    obj = await repo.get_dog(db, dog_id)
    if obj is None:
        raise HTTPException(404, "Собака не найдена")
    return obj


@router.put(
    "/{dog_id}",
    response_model=DogResponse,
    summary="Обновить собаку",
)
async def update_dog(
    dog_id: uuid.UUID,
    body: DogUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return await svc.update_dog(
            db,
            dog_id=dog_id,
            requester_id=user.id,
            is_admin=_is_admin(user),
            fields=body.model_dump(exclude_unset=True),
        )
    except ValueError as e:
        _raise_for_error(e)


@router.get(
    "/{dog_id}/pedigree",
    response_model=PedigreeNode,
    summary="Родословная (3 поколения)",
)
async def get_pedigree(
    dog_id: uuid.UUID,
    generations: int = Query(3, ge=1, le=5),
    db: AsyncSession = Depends(get_db),
):
    node = await svc.build_pedigree(db, dog_id, generations=generations)
    if node is None:
        raise HTTPException(404, "Собака не найдена")
    return node
