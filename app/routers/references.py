"""
Публичные GET-эндпоинты справочников (этап 3).

Доступны без авторизации: справочники нужны и анонимному пользователю
(посетителю каталога пород), и фронту для рендера выпадающих списков.

Маршруты сгруппированы под одним APIRouter с префиксом /references,
чтобы оформление было единообразным и не плодить router-ов на каждую
сущность.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.progressive_ban import check_rate_limit
from app.redis import get_redis
from app.repositories import reference as repo
from app.schemas.reference import (
    AnimalTypeResponse,
    BreedGroupResponse,
    BreedPage,
    BreedResponse,
    GradeResponse,
    PageMeta,
    ShowClassResponse,
    ShowRankResponse,
    TitleResponse,
)

router = APIRouter(prefix="/references", tags=["references"])


@router.get(
    "/animal-types",
    response_model=list[AnimalTypeResponse],
    summary="Виды животных",
)
async def list_animal_types(db: AsyncSession = Depends(get_db)):
    return await repo.list_animal_types(db)


@router.get(
    "/breed-groups",
    response_model=list[BreedGroupResponse],
    summary="Группы пород (FCI 1..10 для собак)",
)
async def list_breed_groups(
    animal_type_id: uuid.UUID | None = Query(
        None, description="Если задан — вернуть только группы этого вида"
    ),
    db: AsyncSession = Depends(get_db),
):
    return await repo.list_breed_groups(db, animal_type_id=animal_type_id)


@router.get(
    "/breeds",
    response_model=BreedPage,
    summary="Список пород с фильтром и пагинацией",
    description=(
        "Фильтр по виду и группе, поиск по имени. Пагинация limit/offset: "
        "page=1 — первая страница, per_page до 200. Возвращает items + meta "
        "с общим количеством — клиент не делает отдельный запрос за total."
    ),
)
async def list_breeds(
    request: Request,
    animal_type_id: uuid.UUID | None = Query(None),
    breed_group_id: uuid.UUID | None = Query(None),
    search: str | None = Query(None, max_length=128),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    # bug_219 audit 2026-05-28: эндпоинт public, без auth, c per_page
    # до 200 + поиск через ILIKE — это самый «тяжёлый» из справочников.
    # 60 req/min на IP отрезает скрейперы, не мешая обычному фронту
    # (один запрос на страницу + дозагрузка).
    await check_rate_limit(
        request,
        limit=60,
        window=60,
        redis=redis,
    )
    items = await repo.list_breeds(
        db,
        animal_type_id=animal_type_id,
        breed_group_id=breed_group_id,
        search=search,
        page=page,
        per_page=per_page,
    )
    total = await repo.count_breeds(
        db,
        animal_type_id=animal_type_id,
        breed_group_id=breed_group_id,
        search=search,
    )
    return BreedPage(
        items=[BreedResponse.model_validate(b) for b in items],
        meta=PageMeta(total=total, page=page, per_page=per_page),
    )


@router.get(
    "/breeds/{breed_id}",
    response_model=BreedResponse,
    summary="Порода по id",
)
async def get_breed(breed_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    obj = await repo.get_breed(db, breed_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="Порода не найдена")
    return obj


@router.get(
    "/show-classes",
    response_model=list[ShowClassResponse],
    summary="Выставочные классы",
)
async def list_show_classes(
    animal_type_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    return await repo.list_show_classes(db, animal_type_id=animal_type_id)


@router.get(
    "/show-ranks",
    response_model=list[ShowRankResponse],
    summary="Ранги выставок (CACIB, CAC ЧРКФ и т.д.)",
)
async def list_show_ranks(db: AsyncSession = Depends(get_db)):
    return await repo.list_show_ranks(db)


@router.get(
    "/titles",
    response_model=list[TitleResponse],
    summary="Титулы",
)
async def list_titles(
    animal_type_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    return await repo.list_titles(db, animal_type_id=animal_type_id)


@router.get(
    "/grades",
    response_model=list[GradeResponse],
    summary="Оценки эксперта",
)
async def list_grades(
    animal_type_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    return await repo.list_grades(db, animal_type_id=animal_type_id)
