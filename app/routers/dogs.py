"""
Роутер собак (этап 4).
"""

from __future__ import annotations

import uuid
from typing import Literal, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.dog import Dog, DogPhoto, SexEnum
from app.models.user import User
from app.repositories import dog as repo
from app.repositories import result as result_repo
from app.schemas.dog import (
    DogCreate,
    DogImageCreate,
    DogPage,
    DogResponse,
    DogUpdate,
    PedigreeNode,
)
from app.schemas.result import DogTitleResponse
from app.services import dog as svc

router = APIRouter(prefix="/dogs", tags=["dogs"])


def _is_admin(user: User) -> bool:
    return any(r.role.value == "admin" for r in user.roles)


def _dog_response(dog: Dog, photos: list[DogPhoto]) -> DogResponse:
    """DogResponse + фото (avatar_file_id из is_primary/первого, галерея по position)."""
    return DogResponse.from_orm_with_photos(dog, photos)


def _raise_for_error(err: ValueError) -> NoReturn:
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
        dog = await svc.create_dog(
            db,
            requester_id=user.id,
            is_admin=_is_admin(user),
            fields=body.model_dump(),
        )
    except ValueError as e:
        _raise_for_error(e)
    return _dog_response(dog, await repo.list_dog_photos(db, dog.id))


@router.get(
    "",
    response_model=DogPage,
    summary="Поиск собак",
    description="Фильтры: порода, питомник, пол; поиск по имени; пагинация.",
)
async def list_dogs(
    breed_id: uuid.UUID | None = Query(None),
    kennel_id: uuid.UUID | None = Query(None),
    litter_id: uuid.UUID | None = Query(None),
    sex: SexEnum | None = Query(None),
    search: str | None = Query(None, max_length=128),
    sort_by: Literal["name", "date_of_birth", "created_at"] = Query("name"),
    order: Literal["asc", "desc"] = Query("asc"),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    items = await repo.list_dogs(
        db,
        breed_id=breed_id,
        kennel_id=kennel_id,
        litter_id=litter_id,
        sex=sex,
        search=search,
        sort_by=sort_by,
        order=order,
        page=page,
        per_page=per_page,
    )
    total = await repo.count_dogs(
        db, breed_id=breed_id, kennel_id=kennel_id, litter_id=litter_id,
        sex=sex, search=search,
    )
    # Фото пачкой (анти-N+1).
    photos = await repo.photos_by_dogs(db, [d.id for d in items])
    return DogPage(
        items=[_dog_response(d, photos.get(d.id, [])) for d in items],
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
    return _dog_response(obj, await repo.list_dog_photos(db, obj.id))


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
        dog = await svc.update_dog(
            db,
            dog_id=dog_id,
            requester_id=user.id,
            is_admin=_is_admin(user),
            fields=body.model_dump(exclude_unset=True),
        )
    except ValueError as e:
        _raise_for_error(e)
    return _dog_response(dog, await repo.list_dog_photos(db, dog.id))


@router.post(
    "/{dog_id}/images",
    response_model=DogResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Добавить фото к собаке",
    description=(
        "Привязывает уже загруженные файлы (file_id из POST /files/upload) "
        "к собаке. Только владелец питомника собаки или admin. На дубликат "
        "пары (dog_id, file_id) БД вернёт 409."
    ),
)
async def add_dog_images(
    dog_id: uuid.UUID,
    body: list[DogImageCreate],
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        dog = await svc.add_images(
            db,
            dog_id=dog_id,
            requester_id=user.id,
            is_admin=_is_admin(user),
            images=[img.model_dump() for img in body],
        )
    except ValueError as e:
        _raise_for_error(e)
    return _dog_response(dog, await repo.list_dog_photos(db, dog.id))


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


@router.get(
    "/{dog_id}/titles",
    response_model=list[DogTitleResponse],
    summary="Все титулы собаки",
    description=(
        "Возвращает список присвоенных собаке титулов (источник истины — "
        "таблица dog_titles, заполняется автоматически при вводе результатов "
        "выставок на этапе 7)."
    ),
)
async def list_titles(
    dog_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    return await result_repo.list_dog_titles(db, dog_id)
