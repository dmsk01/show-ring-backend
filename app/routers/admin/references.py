"""
Админ-CRUD справочников (этап 3).

Все мутации справочников требуют роли admin. На уровне маршрута
закрепляем это одним Depends на роутер — чтобы случайно не забыть
require_any_role на новом эндпоинте.

Ошибки сервиса (ValueError с code-строкой) мапим на HTTP-коды:
- not_found / *_not_found  → 404
- duplicate_code           → 409
- ref_in_use               → 409 (нельзя удалить, есть ссылки)
- animal_type_mismatch     → 422 (порода и группа разных видов)

Сервис уже коммитит транзакцию; роутер только переводит исключения.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import require_any_role
from app.schemas.reference import (
    AnimalTypeCreate,
    AnimalTypeResponse,
    AnimalTypeUpdate,
    BreedCreate,
    BreedGroupCreate,
    BreedGroupResponse,
    BreedGroupUpdate,
    BreedResponse,
    BreedUpdate,
    GradeCreate,
    GradeResponse,
    GradeUpdate,
    ShowClassCreate,
    ShowClassResponse,
    ShowClassUpdate,
    ShowRankCreate,
    ShowRankResponse,
    ShowRankUpdate,
    TitleCreate,
    TitleResponse,
    TitleUpdate,
)
from app.services import reference as svc

# dependencies на уровне роутера — одна точка проверки роли.
# Без этого пришлось бы в каждом @router.post / put / delete явно
# писать Depends(require_any_role("admin")) и легко было бы забыть.
router = APIRouter(
    prefix="/admin/references",
    tags=["admin-references"],
    dependencies=[Depends(require_any_role("admin"))],
)


# ---------------------------------------------------------------------
# AnimalType
# ---------------------------------------------------------------------


@router.post(
    "/animal-types",
    response_model=AnimalTypeResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_animal_type(
    body: AnimalTypeCreate,
    db: AsyncSession = Depends(get_db),
):
    return await svc.create_animal_type(db, **body.model_dump())


@router.put("/animal-types/{id_}", response_model=AnimalTypeResponse)
async def update_animal_type(
    id_: uuid.UUID,
    body: AnimalTypeUpdate,
    db: AsyncSession = Depends(get_db),
):
    # exclude_unset — НЕ путать с exclude_none. unset=поля, которые
    # клиент НЕ присылал, none=присланные с null. На PUT обычно
    # клиент явно присылает null, чтобы сбросить значение.
    return await svc.update_animal_type(db, id_, body.model_dump(exclude_unset=True))


@router.delete("/animal-types/{id_}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_animal_type(id_: uuid.UUID, db: AsyncSession = Depends(get_db)):
    await svc.delete_animal_type(db, id_)


# ---------------------------------------------------------------------
# BreedGroup
# ---------------------------------------------------------------------


@router.post(
    "/breed-groups",
    response_model=BreedGroupResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_breed_group(
    body: BreedGroupCreate, db: AsyncSession = Depends(get_db)
):
    return await svc.create_breed_group(db, **body.model_dump())


@router.put("/breed-groups/{id_}", response_model=BreedGroupResponse)
async def update_breed_group(
    id_: uuid.UUID,
    body: BreedGroupUpdate,
    db: AsyncSession = Depends(get_db),
):
    return await svc.update_breed_group(db, id_, body.model_dump(exclude_unset=True))


@router.delete("/breed-groups/{id_}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_breed_group(id_: uuid.UUID, db: AsyncSession = Depends(get_db)):
    await svc.delete_breed_group(db, id_)


# ---------------------------------------------------------------------
# Breed
# ---------------------------------------------------------------------


@router.post(
    "/breeds",
    response_model=BreedResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_breed(body: BreedCreate, db: AsyncSession = Depends(get_db)):
    return await svc.create_breed(db, **body.model_dump())


@router.put("/breeds/{id_}", response_model=BreedResponse)
async def update_breed(
    id_: uuid.UUID, body: BreedUpdate, db: AsyncSession = Depends(get_db)
):
    return await svc.update_breed(db, id_, body.model_dump(exclude_unset=True))


@router.delete("/breeds/{id_}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_breed(id_: uuid.UUID, db: AsyncSession = Depends(get_db)):
    await svc.delete_breed(db, id_)


# ---------------------------------------------------------------------
# ShowClass
# ---------------------------------------------------------------------


@router.post(
    "/show-classes",
    response_model=ShowClassResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_show_class(
    body: ShowClassCreate, db: AsyncSession = Depends(get_db)
):
    return await svc.create_show_class(db, **body.model_dump())


@router.put("/show-classes/{id_}", response_model=ShowClassResponse)
async def update_show_class(
    id_: uuid.UUID,
    body: ShowClassUpdate,
    db: AsyncSession = Depends(get_db),
):
    return await svc.update_show_class(db, id_, body.model_dump(exclude_unset=True))


@router.delete("/show-classes/{id_}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_show_class(id_: uuid.UUID, db: AsyncSession = Depends(get_db)):
    await svc.delete_show_class(db, id_)


# ---------------------------------------------------------------------
# ShowRank
# ---------------------------------------------------------------------


@router.post(
    "/show-ranks",
    response_model=ShowRankResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_show_rank(body: ShowRankCreate, db: AsyncSession = Depends(get_db)):
    return await svc.create_show_rank(db, **body.model_dump())


@router.put("/show-ranks/{id_}", response_model=ShowRankResponse)
async def update_show_rank(
    id_: uuid.UUID, body: ShowRankUpdate, db: AsyncSession = Depends(get_db)
):
    return await svc.update_show_rank(db, id_, body.model_dump(exclude_unset=True))


@router.delete("/show-ranks/{id_}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_show_rank(id_: uuid.UUID, db: AsyncSession = Depends(get_db)):
    await svc.delete_show_rank(db, id_)


# ---------------------------------------------------------------------
# Title
# ---------------------------------------------------------------------


@router.post(
    "/titles", response_model=TitleResponse, status_code=status.HTTP_201_CREATED
)
async def create_title(body: TitleCreate, db: AsyncSession = Depends(get_db)):
    return await svc.create_title(db, **body.model_dump())


@router.put("/titles/{id_}", response_model=TitleResponse)
async def update_title(
    id_: uuid.UUID, body: TitleUpdate, db: AsyncSession = Depends(get_db)
):
    return await svc.update_title(db, id_, body.model_dump(exclude_unset=True))


@router.delete("/titles/{id_}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_title(id_: uuid.UUID, db: AsyncSession = Depends(get_db)):
    await svc.delete_title(db, id_)


# ---------------------------------------------------------------------
# Grade
# ---------------------------------------------------------------------


@router.post(
    "/grades", response_model=GradeResponse, status_code=status.HTTP_201_CREATED
)
async def create_grade(body: GradeCreate, db: AsyncSession = Depends(get_db)):
    return await svc.create_grade(db, **body.model_dump())


@router.put("/grades/{id_}", response_model=GradeResponse)
async def update_grade(
    id_: uuid.UUID, body: GradeUpdate, db: AsyncSession = Depends(get_db)
):
    return await svc.update_grade(db, id_, body.model_dump(exclude_unset=True))


@router.delete("/grades/{id_}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_grade(id_: uuid.UUID, db: AsyncSession = Depends(get_db)):
    await svc.delete_grade(db, id_)
