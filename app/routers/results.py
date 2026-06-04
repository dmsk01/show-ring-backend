"""
Роутер результатов и публикации (этап 7).

Эндпоинты, привязанные к выставке: ввод оценок, выбор лучших на разных
уровнях, публикация результатов. Отдельный модуль ради лаконичности
shows.py.
"""

from __future__ import annotations

import uuid
from typing import NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.show import ShowEntry
from app.models.user import User
from app.repositories import result as repo
from app.schemas.result import (
    BestInGroupRequest,
    BestInShowRequest,
    BestOfBreedRequest,
    ShowResultCreate,
    ShowResultResponse,
    ShowResultUpdate,
)
from app.schemas.show import ShowResponse
from app.services import result as svc

router = APIRouter(prefix="/shows/{show_id}/results", tags=["results"])


def _is_admin(user: User) -> bool:
    return any(r.role.value == "admin" for r in user.roles)


def _raise_for_error(err: ValueError) -> NoReturn:
    code = str(err)
    not_found = {
        "not_found",
        "entry_not_found",
        "result_not_found",
        "show_class_not_found",
        "show_rank_not_found",
        "grade_not_found",
    }
    if code in not_found:
        raise HTTPException(404, code)
    if code == "forbidden":
        raise HTTPException(403, code)
    if code in (
        "show_not_in_progress",
        "invalid_status_transition",
        "entry_breed_mismatch",
        "entry_show_mismatch",
        "entry_group_mismatch",
        "winner_must_be_bob",
        "winner_must_be_big",
    ):
        raise HTTPException(422, code)
    raise HTTPException(400, code)


# ---------------------------------------------------------------------
# Базовые результаты
# ---------------------------------------------------------------------


@router.post(
    "",
    response_model=ShowResultResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Ввести/обновить результат в ринге",
    description=(
        "Создаёт результат для записи (ShowEntry). Если результат уже был — "
        "обновляет поля. На placement=1 + grade=excellent в can_receive_cac "
        "классе автоматически выдаёт CW + CAC, в юниорах добавляет ЮСАС."
    ),
)
async def upsert_result(
    show_id: uuid.UUID,  # noqa: ARG001 — нужен в URL, проверка через entry
    body: ShowResultCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return await svc.upsert_class_result(
            db,
            show_entry_id=body.show_entry_id,
            user_id=user.id,
            is_admin=_is_admin(user),
            grade_id=body.grade_id,
            placement=body.placement,
            critique=body.critique,
        )
    except ValueError as e:
        _raise_for_error(e)


@router.put(
    "/{result_id}",
    response_model=ShowResultResponse,
    summary="Скорректировать результат",
)
async def update_result(
    show_id: uuid.UUID,
    result_id: uuid.UUID,
    body: ShowResultUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # Update идёт через тот же upsert_class_result — мы знаем entry по
    # result_id.
    result = await repo.get_result(db, result_id)
    if result is None:
        raise HTTPException(404, "result_not_found")
    # ИСПРАВЛЕНО (bug_208 audit 2026-05-28): без этой проверки URL
    # `/shows/X/results/<result_из_Y>` шёл на upsert_class_result, который
    # выбирал show из entry (т.е. Y), а URL-параметр show_id оставался
    # декоративным. Атакующий-судья выставки Y мог дёрнуть эндпоинт с
    # любым show_id в URL и обойти любые middleware-проверки/политики,
    # завязанные на URL-pattern. Сравниваем entry.show_id с URL-значением,
    # отдаём 404 (не 403, чтобы не раскрывать факт существования result'а
    # в чужой выставке).
    entry = await db.get(ShowEntry, result.show_entry_id)
    if entry is None or entry.show_id != show_id:
        raise HTTPException(404, "result_not_found")
    try:
        return await svc.upsert_class_result(
            db,
            show_entry_id=result.show_entry_id,
            user_id=user.id,
            is_admin=_is_admin(user),
            grade_id=body.grade_id,
            placement=body.placement,
            critique=body.critique,
        )
    except ValueError as e:
        _raise_for_error(e)


@router.delete(
    "/{result_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Удалить результат (с отзывом выданных титулов)",
)
async def delete_result(
    show_id: uuid.UUID,
    result_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        await svc.delete_result(
            db,
            show_id=show_id,
            result_id=result_id,
            user_id=user.id,
            is_admin=_is_admin(user),
        )
    except ValueError as e:
        _raise_for_error(e)


@router.get(
    "",
    response_model=list[ShowResultResponse],
    summary="Все результаты выставки",
)
async def list_results(
    show_id: uuid.UUID,
    page: int = Query(1, ge=1),
    per_page: int = Query(200, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
):
    items = await repo.list_results_for_show(
        db, show_id, page=page, per_page=per_page
    )
    return [ShowResultResponse.model_validate(r) for r in items]


@router.get(
    "/by-breed/{breed_id}",
    response_model=list[ShowResultResponse],
    summary="Результаты по породе",
)
async def list_results_by_breed(
    show_id: uuid.UUID,
    breed_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    items = await repo.list_results_by_breed(db, show_id, breed_id)
    return [ShowResultResponse.model_validate(r) for r in items]


@router.get(
    "/by-ring",
    response_model=list[ShowResultResponse],
    summary="Результаты ринга (порода + класс)",
)
async def list_results_by_ring(
    show_id: uuid.UUID,
    breed_id: uuid.UUID | None = Query(None),
    show_class_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    items = await repo.list_results_by_ring(
        db, show_id, breed_id, show_class_id
    )
    return [ShowResultResponse.model_validate(r) for r in items]


# ---------------------------------------------------------------------
# Лучшие
# ---------------------------------------------------------------------


@router.post(
    "/best-of-breed",
    response_model=ShowResultResponse,
    summary="Определить ЛПП (BOB) для породы",
)
async def best_of_breed(
    show_id: uuid.UUID,
    body: BestOfBreedRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return await svc.set_best_of_breed(
            db,
            show_id=show_id,
            user_id=user.id,
            is_admin=_is_admin(user),
            breed_id=body.breed_id,
            winner_entry_id=body.winner_entry_id,
            best_male_entry_id=body.best_male_entry_id,
            best_female_entry_id=body.best_female_entry_id,
            best_junior_entry_id=body.best_junior_entry_id,
            best_veteran_entry_id=body.best_veteran_entry_id,
        )
    except ValueError as e:
        _raise_for_error(e)


@router.post(
    "/best-in-group",
    response_model=ShowResultResponse,
    summary="Определить BIG для группы FCI",
)
async def best_in_group(
    show_id: uuid.UUID,
    body: BestInGroupRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return await svc.set_best_in_group(
            db,
            show_id=show_id,
            user_id=user.id,
            is_admin=_is_admin(user),
            breed_group_id=body.breed_group_id,
            winner_entry_id=body.winner_entry_id,
        )
    except ValueError as e:
        _raise_for_error(e)


@router.post(
    "/best-in-show",
    response_model=ShowResultResponse,
    summary="Определить BIS — победителя выставки",
)
async def best_in_show(
    show_id: uuid.UUID,
    body: BestInShowRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return await svc.set_best_in_show(
            db,
            show_id=show_id,
            user_id=user.id,
            is_admin=_is_admin(user),
            winner_entry_id=body.winner_entry_id,
        )
    except ValueError as e:
        _raise_for_error(e)


# ---------------------------------------------------------------------
# Публикация
# ---------------------------------------------------------------------


# Отдельный mini-роутер, чтобы /shows/{id}/publish не попадал внутрь
# префикса /results.
publish_router = APIRouter(prefix="/shows", tags=["results"])


@publish_router.post(
    "/{show_id}/publish",
    response_model=ShowResponse,
    summary="Опубликовать результаты (status → completed)",
)
async def publish(
    show_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return await svc.publish_results(
            db,
            show_id=show_id,
            user_id=user.id,
            is_admin=_is_admin(user),
        )
    except ValueError as e:
        _raise_for_error(e)
