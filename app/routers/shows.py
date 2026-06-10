"""
Роутер выставок (этап 6).

Группа эндпоинтов сосредоточена в одном модуле:
- CRUD выставок и смена статуса,
- управление судьями и рингами,
- запись собак (с выбором класса владельцем).
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Literal, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user, is_admin, require_any_role
from app.models.show import ShowStatus
from app.models.user import User
from app.repositories import show as repo
from app.schemas.show import (
    AvailableClass,
    AvailableClassesResponse,
    MyShowEntryResponse,
    MyShowItem,
    MyShowPage,
    ShowCreate,
    ShowEntryCreate,
    ShowEntryPage,
    ShowEntryResponse,
    ShowEntryUpdate,
    ShowJudgeCreate,
    ShowJudgeResponse,
    ShowPage,
    ShowResponse,
    ShowRingCreate,
    ShowRingResponse,
    ShowRingUpdate,
    ShowStatusUpdate,
    ShowUpdate,
)
from app.services import show as svc

router = APIRouter(prefix="/shows", tags=["shows"])


# ИСПРАВЛЕНО (review 2026-05-28): см. routers/classifieds.py.
_is_admin = is_admin


def _raise_for_error(err: ValueError) -> NoReturn:
    """
    NoReturn — функция всегда кидает HTTPException. Аннотация нужна,
    чтобы pyright/mypy понимали, что код после её вызова недостижим
    (иначе кричит на "переменные могут быть unbound" в try/except-блоках).
    """
    code = str(err)
    not_found_codes = {
        "not_found",
        "dog_not_found",
        "breed_not_found",
        "show_class_not_found",
        "judge_assignment_not_found",
        "ring_not_found",
        "entry_not_found",
    }
    if code in not_found_codes:
        raise HTTPException(404, code)
    if code == "forbidden":
        raise HTTPException(403, code)
    if code in ("dog_already_registered",):
        raise HTTPException(409, code)
    if code in (
        "registration_not_open",
        "registration_deadline_passed",
        "registration_locked",
        "show_locked",
        "invalid_status_transition",
        "breed_not_allowed",
        "class_not_available_for_age",
        "class_animal_type_mismatch",
        "dog_birth_date_missing",
    ):
        raise HTTPException(422, code)
    raise HTTPException(400, code)


# ---------------------------------------------------------------------
# Shows: CRUD
# ---------------------------------------------------------------------


@router.post(
    "",
    response_model=ShowResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Создать выставку",
)
async def create_show(
    body: ShowCreate,
    db: AsyncSession = Depends(get_db),
    # ИСПРАВЛЕНО (review 2026-06-10): докстринг сервиса всегда обещал
    # «создавать может только organizer или admin», но проверки не было —
    # выставку мог открыть любой свежий аккаунт.
    user: User = Depends(require_any_role("organizer", "admin")),
):
    return await svc.create_show(
        db, organizer_id=user.id, fields=body.model_dump()
    )


@router.get(
    "",
    response_model=ShowPage,
    summary="Список выставок",
)
async def list_shows(
    rank_id: uuid.UUID | None = Query(None),
    city: str | None = Query(None, max_length=128),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    status_: ShowStatus | None = Query(None, alias="status"),
    sort_by: Literal["date_start", "created_at"] = Query("date_start"),
    order: Literal["asc", "desc"] = Query("asc"),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    items = await repo.list_shows(
        db,
        rank_id=rank_id,
        city=city,
        date_from=date_from,
        date_to=date_to,
        status=status_,
        sort_by=sort_by,
        order=order,
        page=page,
        per_page=per_page,
    )
    total = await repo.count_shows(
        db,
        rank_id=rank_id,
        city=city,
        date_from=date_from,
        date_to=date_to,
        status=status_,
    )
    return ShowPage(
        items=[ShowResponse.model_validate(s) for s in items],
        total=total,
        page=page,
        per_page=per_page,
    )


@router.get(
    "/{show_id}",
    response_model=ShowResponse,
    summary="Карточка выставки",
)
async def get_show(show_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    obj = await repo.get_show(db, show_id)
    if obj is None:
        raise HTTPException(404, "Выставка не найдена")
    return obj


@router.put(
    "/{show_id}",
    response_model=ShowResponse,
    summary="Обновить выставку",
)
async def update_show(
    show_id: uuid.UUID,
    body: ShowUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return await svc.update_show(
            db,
            show_id=show_id,
            requester_id=user.id,
            is_admin=_is_admin(user),
            fields=body.model_dump(exclude_unset=True),
        )
    except ValueError as e:
        _raise_for_error(e)


@router.put(
    "/{show_id}/status",
    response_model=ShowResponse,
    summary="Сменить статус выставки",
)
async def change_status(
    show_id: uuid.UUID,
    body: ShowStatusUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return await svc.change_status(
            db,
            show_id=show_id,
            requester_id=user.id,
            is_admin=_is_admin(user),
            target=body.status,
        )
    except ValueError as e:
        _raise_for_error(e)


@router.delete(
    "/{show_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Удалить выставку",
)
async def delete_show(
    show_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        await svc.delete_show(
            db,
            show_id=show_id,
            requester_id=user.id,
            is_admin=_is_admin(user),
        )
    except ValueError as e:
        _raise_for_error(e)


# ---------------------------------------------------------------------
# Judges
# ---------------------------------------------------------------------


@router.post(
    "/{show_id}/judges",
    response_model=ShowJudgeResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Назначить судью",
)
async def add_judge(
    show_id: uuid.UUID,
    body: ShowJudgeCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return await svc.add_judge(
            db,
            show_id=show_id,
            requester_id=user.id,
            is_admin=_is_admin(user),
            judge_id=body.judge_id,
            breed_id=body.breed_id,
            breed_group_id=body.breed_group_id,
        )
    except ValueError as e:
        _raise_for_error(e)


@router.get(
    "/{show_id}/judges",
    response_model=list[ShowJudgeResponse],
    summary="Список судей выставки",
)
async def list_judges(
    show_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    return await repo.list_show_judges(db, show_id)


@router.delete(
    "/{show_id}/judges/{judge_record_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Убрать назначение судьи",
)
async def remove_judge(
    show_id: uuid.UUID,
    judge_record_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        await svc.remove_judge(
            db,
            show_id=show_id,
            judge_record_id=judge_record_id,
            requester_id=user.id,
            is_admin=_is_admin(user),
        )
    except ValueError as e:
        _raise_for_error(e)


# ---------------------------------------------------------------------
# Rings
# ---------------------------------------------------------------------


@router.post(
    "/{show_id}/rings",
    response_model=ShowRingResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Создать ринг",
)
async def add_ring(
    show_id: uuid.UUID,
    body: ShowRingCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return await svc.add_ring(
            db,
            show_id=show_id,
            requester_id=user.id,
            is_admin=_is_admin(user),
            fields=body.model_dump(),
        )
    except ValueError as e:
        _raise_for_error(e)


@router.get(
    "/{show_id}/rings",
    response_model=list[ShowRingResponse],
    summary="Расписание рингов",
)
async def list_rings(
    show_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    return await repo.list_show_rings(db, show_id)


@router.put(
    "/{show_id}/rings/{ring_id}",
    response_model=ShowRingResponse,
    summary="Обновить ринг",
)
async def update_ring(
    show_id: uuid.UUID,
    ring_id: uuid.UUID,
    body: ShowRingUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return await svc.update_ring(
            db,
            show_id=show_id,
            ring_id=ring_id,
            requester_id=user.id,
            is_admin=_is_admin(user),
            fields=body.model_dump(exclude_unset=True),
        )
    except ValueError as e:
        _raise_for_error(e)


@router.delete(
    "/{show_id}/rings/{ring_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Удалить ринг",
)
async def delete_ring(
    show_id: uuid.UUID,
    ring_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        await svc.delete_ring(
            db,
            show_id=show_id,
            ring_id=ring_id,
            requester_id=user.id,
            is_admin=_is_admin(user),
        )
    except ValueError as e:
        _raise_for_error(e)


# ---------------------------------------------------------------------
# Available classes
# ---------------------------------------------------------------------


@router.get(
    "/{show_id}/available-classes/{dog_id}",
    response_model=AvailableClassesResponse,
    summary="Доступные классы для собаки",
    description=(
        "Возвращает список классов выставки, в которые собака может быть "
        "записана по возрасту на дату выставки. Владелец выбирает один "
        "из списка при создании записи."
    ),
)
async def get_available_classes(
    show_id: uuid.UUID,
    dog_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),  # noqa: ARG001
):
    try:
        dog, classes, age_months = await svc.get_available_classes_for_dog(
            db, show_id=show_id, dog_id=dog_id
        )
    except ValueError as e:
        _raise_for_error(e)
    return AvailableClassesResponse(
        dog_id=dog.id,
        age_at_show_months=age_months,
        classes=[
            AvailableClass(
                id=c.id,
                code=c.code,
                name=c.name,
                age_from_months=c.age_from_months,
                age_to_months=c.age_to_months,
                can_receive_cac=c.can_receive_cac,
                requires_documents=c.requires_documents,
                documents_note=c.documents_note,
            )
            for c in classes
        ],
    )


# ---------------------------------------------------------------------
# Entries
# ---------------------------------------------------------------------


@router.post(
    "/{show_id}/entries",
    response_model=ShowEntryResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Записать собаку на выставку",
)
async def create_entry(
    show_id: uuid.UUID,
    body: ShowEntryCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return await svc.register_entry(
            db,
            show_id=show_id,
            requester_id=user.id,
            is_admin=_is_admin(user),
            dog_id=body.dog_id,
            show_class_id=body.show_class_id,
            handler_id=body.handler_id,
            notes=body.notes,
            today=date.today(),
        )
    except ValueError as e:
        _raise_for_error(e)


# Путь без {show_id} (два сегмента после /shows), поэтому с
# GET /shows/{show_id} не конфликтует.
@router.get(
    "/entries/my",
    response_model=MyShowPage,
    summary="Мои выставки (где у меня есть запись)",
)
async def list_my_shows(
    status_group: str = Query("all", pattern="^(all|active|past)$"),
    page: int = Query(1, ge=1),
    per_page: int = Query(12, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rows = await repo.list_my_shows(
        db, user.id, status_group, page=page, per_page=per_page
    )
    total = await repo.count_my_shows(db, user.id, status_group)
    items = [
        MyShowItem(
            **ShowResponse.model_validate(show).model_dump(),
            my_entries_count=cnt,
        )
        for show, cnt in rows
    ]
    return MyShowPage(items=items, total=total, page=page, per_page=per_page)


# /entries/my должен идти ДО /entries/{eid}, иначе FastAPI попробует
# распарсить "my" как UUID.
@router.get(
    "/{show_id}/entries/my",
    response_model=list[MyShowEntryResponse],
    summary="Мои записи на эту выставку",
)
async def list_my_entries(
    show_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rows = await repo.list_user_entries_for_show_enriched(db, show_id, user.id)
    return [
        MyShowEntryResponse(
            **ShowEntryResponse.model_validate(entry).model_dump(),
            dog_name=dog_name,
            class_code=class_code,
            class_name=class_name,
        )
        for entry, dog_name, class_code, class_name in rows
    ]


@router.get(
    "/{show_id}/entries",
    response_model=ShowEntryPage,
    summary="Каталог записей",
)
async def list_entries(
    show_id: uuid.UUID,
    page: int = Query(1, ge=1),
    per_page: int = Query(200, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
):
    items = await repo.list_show_entries(
        db, show_id, page=page, per_page=per_page
    )
    total = await repo.count_show_entries(db, show_id)
    return ShowEntryPage(
        items=[ShowEntryResponse.model_validate(e) for e in items],
        total=total,
        page=page,
        per_page=per_page,
    )


@router.delete(
    "/{show_id}/entries/{entry_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Отменить запись",
)
async def cancel_entry(
    show_id: uuid.UUID,
    entry_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        await svc.cancel_entry(
            db,
            show_id=show_id,
            entry_id=entry_id,
            requester_id=user.id,
            is_admin=_is_admin(user),
        )
    except ValueError as e:
        _raise_for_error(e)


@router.patch(
    "/{show_id}/entries/{entry_id}",
    response_model=MyShowEntryResponse,
    summary="Изменить свою запись",
)
async def update_entry(
    show_id: uuid.UUID,
    entry_id: uuid.UUID,
    body: ShowEntryUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        await svc.update_entry(
            db,
            show_id=show_id,
            entry_id=entry_id,
            requester_id=user.id,
            is_admin=_is_admin(user),
            show_class_id=body.show_class_id,
            handler_id=body.handler_id,
            notes=body.notes,
            today=date.today(),
        )
    except ValueError as e:
        _raise_for_error(e)
    row = await repo.get_entry_enriched(db, entry_id)
    if row is None:  # запись только что обновили; None возможен лишь в гонке
        raise HTTPException(404, "entry_not_found")
    entry, dog_name, class_code, class_name = row
    return MyShowEntryResponse(
        **ShowEntryResponse.model_validate(entry).model_dump(),
        dog_name=dog_name,
        class_code=class_code,
        class_name=class_name,
    )
