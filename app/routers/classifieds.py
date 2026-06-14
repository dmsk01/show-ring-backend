"""
Роутер доски объявлений (этап 5).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user, is_admin
from app.middleware.progressive_ban import check_rate_limit
from app.models.classified import (
    AnimalAvailability,
    ClassifiedCategory,
    ClassifiedStatus,
)
from app.models.dog import SexEnum
from app.models.user import User
from app.redis import get_redis
from app.repositories import classified as repo
from app.schemas.classified import (
    ClassifiedCreate,
    ClassifiedImageCreate,
    ClassifiedPage,
    ClassifiedResponse,
    ClassifiedUpdate,
)
from app.services import classified as svc

router = APIRouter(prefix="/classifieds", tags=["classifieds"])


# ИСПРАВЛЕНО (review 2026-05-28): локальный _is_admin вынесен в
# app.dependencies.is_admin. Алиас остаётся, чтобы не править все
# call-site'ы внутри файла.
_is_admin = is_admin


def _raise_for_error(err: ValueError) -> None:
    code = str(err)
    if code == "not_found":
        raise HTTPException(404, code)
    # bug_212/216: file_forbidden — попытка прицепить чужой файл.
    # Тот же класс ошибки, что и обычный forbidden, поэтому 403.
    if code in ("forbidden", "file_forbidden"):
        raise HTTPException(403, code)
    # bug_210: специальный код для запрещённого перехода статуса —
    # 422 (unprocessable), а не 400, потому что это semantic validation
    # ошибка, а не malformed input.
    if code == "status_transition_forbidden":
        raise HTTPException(422, code)
    raise HTTPException(400, code)


@router.post(
    "",
    response_model=ClassifiedResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Создать объявление",
)
async def create_classified(
    body: ClassifiedCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return await svc.create_classified(
        db,
        author_id=user.id,
        is_admin=_is_admin(user),
        fields=body.model_dump(),
    )


# Внимание: /search обязательно ДО /{classified_id}, иначе FastAPI
# попытается интерпретировать "search" как UUID и вернёт 422.
@router.get(
    "/search",
    response_model=ClassifiedPage,
    summary="Полнотекстовый поиск (русский язык)",
    description=(
        "Поиск по title и description с учётом морфологии русского "
        "(snowball-stemmer). Запрос plainto_tsquery — обычный текст, "
        "без специального синтаксиса. Сортировка — по релевантности."
    ),
)
async def search_classifieds(
    request: Request,
    q: str = Query(..., min_length=2, max_length=200),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    # bug_213 audit 2026-05-28: FTS-запрос с 200-символьным q
    # запускает PostgreSQL to_tsquery + GIN-поиск — CPU-стоит. Для
    # анонимного эндпоинта это вектор DoS на каждый запрос.
    # 30 запросов/мин на IP — достаточно для пользовательского поиска
    # (одна-две страницы в секунду), мало для атаки.
    await check_rate_limit(
        request,
        limit=30,
        window=60,
        redis=redis,
    )
    items = await repo.search_classifieds(db, q, page=page, per_page=per_page)
    total = await repo.count_search_results(db, q)
    return ClassifiedPage(
        items=[ClassifiedResponse.model_validate(x) for x in items],
        total=total,
        page=page,
        per_page=per_page,
    )


@router.get(
    "",
    response_model=ClassifiedPage,
    summary="Список объявлений с фильтрами",
)
async def list_classifieds(
    category: ClassifiedCategory | None = Query(None),
    breed_id: uuid.UUID | None = Query(None),
    sex: SexEnum | None = Query(None),
    city: str | None = Query(None, max_length=128),
    availability: AnimalAvailability | None = Query(
        None,
        description=(
            "Фильтр доступности: available (свободен) / reserved "
            "(забронирован) / sold (продан). По умолчанию — все."
        ),
    ),
    price_from: Decimal | None = Query(None, ge=0),
    price_to: Decimal | None = Query(None, ge=0),
    sort_by: Literal["created_at", "price", "views_count"] = Query("created_at"),
    order: Literal["asc", "desc"] = Query("desc"),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    items = await repo.list_classifieds(
        db,
        category=category,
        breed_id=breed_id,
        sex=sex,
        city=city,
        # Публичный список — только активные. Closed/archived не показываем.
        status=ClassifiedStatus.active,
        availability=availability,
        price_from=price_from,
        price_to=price_to,
        sort_by=sort_by,
        order=order,
        page=page,
        per_page=per_page,
    )
    total = await repo.count_classifieds(
        db,
        category=category,
        breed_id=breed_id,
        sex=sex,
        city=city,
        status=ClassifiedStatus.active,
        availability=availability,
        price_from=price_from,
        price_to=price_to,
    )
    return ClassifiedPage(
        items=[ClassifiedResponse.model_validate(x) for x in items],
        total=total,
        page=page,
        per_page=per_page,
    )


@router.get(
    "/mine",
    response_model=ClassifiedPage,
    summary="Мои объявления (все статусы)",
    description=(
        "Объявления текущего пользователя во ВСЕХ статусах (active / "
        "closed / moderation / archived). В отличие от публичного "
        "GET /classifieds, не форсирует status=active и скоупится по "
        "author_id=current_user — поэтому владелец видит и снятые с "
        "публикации, и находящиеся на модерации/в архиве. "
        "Опциональный ?status сужает выборку до одного статуса."
    ),
)
async def list_my_classifieds(
    category: ClassifiedCategory | None = Query(None),
    city: str | None = Query(None, max_length=128),
    status_filter: ClassifiedStatus | None = Query(
        None,
        alias="status",
        description="Фильтр по статусу; не задан — все статусы.",
    ),
    sort_by: Literal["created_at", "price", "views_count"] = Query("created_at"),
    order: Literal["asc", "desc"] = Query("desc"),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    items = await repo.list_classifieds(
        db,
        category=category,
        city=city,
        # None → без фильтра по статусу (все статусы владельца).
        status=status_filter,
        author_id=user.id,
        sort_by=sort_by,
        order=order,
        page=page,
        per_page=per_page,
    )
    total = await repo.count_classifieds(
        db,
        category=category,
        city=city,
        status=status_filter,
        author_id=user.id,
    )
    return ClassifiedPage(
        items=[ClassifiedResponse.model_validate(x) for x in items],
        total=total,
        page=page,
        per_page=per_page,
    )


@router.get(
    "/{classified_id}",
    response_model=ClassifiedResponse,
    summary="Карточка объявления (инкрементирует views_count)",
)
async def get_classified(
    classified_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    obj = await repo.get_classified(db, classified_id, with_images=True)
    if obj is None:
        raise HTTPException(404, "Объявление не найдено")
    # Инкрементируем счётчик просмотров атомарным UPDATE + commit.
    await repo.increment_views(db, classified_id)
    # ВАЖНО: bulk-UPDATE из increment_views экспайрит атрибуты уже
    # загруженного obj (в т.ч. updated_at от onupdate=func.now()). Если
    # вернуть тот же obj, FastAPI при сериализации полезет дочитывать
    # эти поля из БД синхронно — а это IO вне greenlet-контекста →
    # MissingGreenlet → 500. Поэтому перечитываем объект заново (с
    # images) в async-контексте, как это уже делают create/update.
    obj = await repo.get_classified(db, classified_id, with_images=True)
    assert obj is not None  # invariant: только что инкрементировали — точно есть
    return obj


@router.put(
    "/{classified_id}",
    response_model=ClassifiedResponse,
    summary="Обновить объявление",
)
async def update_classified(
    classified_id: uuid.UUID,
    body: ClassifiedUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return await svc.update_classified(
            db,
            classified_id=classified_id,
            requester_id=user.id,
            is_admin=_is_admin(user),
            fields=body.model_dump(exclude_unset=True),
        )
    except ValueError as e:
        _raise_for_error(e)


@router.post(
    "/{classified_id}/images",
    response_model=ClassifiedResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Добавить изображения к объявлению",
    description=(
        "Привязывает уже загруженные файлы (file_id из POST /files/upload) "
        "к существующему объявлению. Только автор/admin. На дубликат "
        "пары (classified_id, file_id) БД вернёт ошибку — клиент должен "
        "избегать повторов."
    ),
)
async def add_images(
    classified_id: uuid.UUID,
    body: list[ClassifiedImageCreate],
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return await svc.add_images(
            db,
            classified_id=classified_id,
            requester_id=user.id,
            is_admin=_is_admin(user),
            images=[img.model_dump() for img in body],
        )
    except ValueError as e:
        _raise_for_error(e)


@router.delete(
    "/{classified_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Закрыть объявление (soft) или удалить из БД (?hard=true)",
)
async def close_classified(
    classified_id: uuid.UUID,
    hard: bool = Query(
        False,
        description=(
            "false (по умолчанию) — мягкое закрытие (status=closed); "
            "true — полное удаление строки из БД (CASCADE на изображения)."
        ),
    ),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        await svc.close_classified(
            db,
            classified_id=classified_id,
            requester_id=user.id,
            is_admin=_is_admin(user),
            hard=hard,
        )
    except ValueError as e:
        _raise_for_error(e)
