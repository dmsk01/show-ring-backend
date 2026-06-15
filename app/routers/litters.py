"""
Роутер помётов (этап 5).
"""

from __future__ import annotations

import uuid
from typing import NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.litter import Litter, LitterStatus
from app.models.user import User
from app.repositories import dog as dog_repo
from app.repositories import litter as repo
from app.schemas.dog import DogRef, DogResponse
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


async def _build_litters(
    db: AsyncSession, litters: list[Litter]
) -> list[LitterResponse]:
    """LitterResponse + развёрнутые father/mother (батч-резолв родителей)."""
    parent_ids = {
        pid for lt in litters for pid in (lt.father_id, lt.mother_id) if pid
    }
    dogs_map = await dog_repo.dogs_by_ids(db, parent_ids)
    photos = await dog_repo.photos_by_dogs(db, parent_ids)

    def _ref(pid: uuid.UUID | None) -> DogRef | None:
        if pid is None:
            return None
        d = dogs_map.get(pid)
        if d is None:
            return None
        ph = photos.get(pid, [])
        avatar = next(
            (p.file_id for p in ph if p.is_primary), None
        ) or (ph[0].file_id if ph else None)
        return DogRef(id=d.id, name=d.name, avatar_file_id=avatar)

    out: list[LitterResponse] = []
    for lt in litters:
        resp = LitterResponse.model_validate(lt)
        resp.father = _ref(lt.father_id)
        resp.mother = _ref(lt.mother_id)
        out.append(resp)
    return out


async def _build_litter(db: AsyncSession, litter: Litter) -> LitterResponse:
    return (await _build_litters(db, [litter]))[0]


def _raise_for_error(err: ValueError) -> NoReturn:
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
        litter = await svc.create_litter(
            db,
            requester_id=user.id,
            is_admin=_is_admin(user),
            fields=body.model_dump(),
        )
    except ValueError as e:
        _raise_for_error(e)
    return await _build_litter(db, litter)


@router.get(
    "",
    response_model=LitterPage,
    summary="Список помётов",
    description="Фильтры: питомник, порода, статус; поиск по названию питомника. Пагинация.",
)
async def list_litters(
    kennel_id: uuid.UUID | None = Query(None),
    breed_id: uuid.UUID | None = Query(None),
    status_: LitterStatus | None = Query(None, alias="status"),
    search: str | None = Query(None, max_length=128),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    # Триммим на границе ввода: "пусто/одни пробелы" → None (без фильтра).
    search = search.strip() or None if search else None
    items = await repo.list_litters(
        db,
        kennel_id=kennel_id,
        breed_id=breed_id,
        status=status_,
        search=search,
        page=page,
        per_page=per_page,
    )
    total = await repo.count_litters(
        db, kennel_id=kennel_id, breed_id=breed_id, status=status_, search=search
    )
    return LitterPage(
        items=await _build_litters(db, list(items)),
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
    return await _build_litter(db, obj)


@router.get(
    "/{litter_id}/puppies",
    response_model=list[DogResponse],
    summary="Щенки помёта",
    description="Собаки, привязанные к помёту через Dog.litter_id (этап 18).",
)
async def list_puppies(
    litter_id: uuid.UUID, db: AsyncSession = Depends(get_db)
):
    dogs = await dog_repo.list_dogs(db, litter_id=litter_id, per_page=200)
    photos = await dog_repo.photos_by_dogs(db, [d.id for d in dogs])
    return [
        DogResponse.from_orm_with_photos(d, photos.get(d.id, [])) for d in dogs
    ]


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
        litter = await svc.update_litter(
            db,
            litter_id=litter_id,
            requester_id=user.id,
            is_admin=_is_admin(user),
            fields=body.model_dump(exclude_unset=True),
        )
    except ValueError as e:
        _raise_for_error(e)
    return await _build_litter(db, litter)


@router.delete(
    "/{litter_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Удалить помёт",
)
async def delete_litter(
    litter_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        await svc.delete_litter(
            db,
            litter_id=litter_id,
            requester_id=user.id,
            is_admin=_is_admin(user),
        )
    except ValueError as e:
        _raise_for_error(e)
