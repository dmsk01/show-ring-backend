"""
Роутер чек-ина (регистрация прибытия на выставку).

Префикс /shows, как у shows.py, но отдельным модулем: shows.py уже
~600 строк. Пути /shows/staff/my и /shows/{id}/... не пересекаются с
маршрутами shows.py (у тех нет второго сегмента staff/checkin/my-ticket).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user, is_admin
from app.middleware.progressive_ban import check_rate_limit
from app.models.user import User
from app.redis import get_redis
from app.schemas.checkin import (
    CheckinSettingsUpdate,
    CheckinSummary,
    EntryCard,
    EntryCheckResponse,
    EntryChecksCreate,
    ParticipantCard,
    ScanRequest,
    ShowStaffAdd,
    ShowStaffResponse,
    TicketResponse,
)
from app.schemas.show import ShowResponse
from app.services import checkin as svc

router = APIRouter(prefix="/shows", tags=["checkin"])


@router.put(
    "/{show_id}/checkin/settings",
    response_model=ShowResponse,
    summary="Включить/выключить чек-ин выставки",
)
async def update_checkin_settings(
    show_id: uuid.UUID,
    body: CheckinSettingsUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return await svc.set_enabled(db, show_id, user.id, is_admin(user), body.enabled)


@router.get("/staff/my", response_model=list[ShowResponse], summary="Выставки, где я регистратор")
async def my_staff_shows(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return await svc.list_my_shows(db, user.id)


@router.get("/{show_id}/staff", response_model=list[ShowStaffResponse], summary="Персонал выставки")
async def list_staff(
    show_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return await svc.list_staff(db, show_id, user.id, is_admin(user))


@router.post(
    "/{show_id}/staff",
    response_model=ShowStaffResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Добавить регистратора по email или телефону",
)
async def add_staff(
    show_id: uuid.UUID,
    body: ShowStaffAdd,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return await svc.add_staff(
        db, show_id, user.id, is_admin(user),
        email=str(body.email) if body.email else None, phone=body.phone,
    )


@router.delete(
    "/{show_id}/staff/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Убрать регистратора",
)
async def remove_staff(
    show_id: uuid.UUID,
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await svc.remove_staff(db, show_id, user.id, is_admin(user), user_id)


_SEARCH_RATE_LIMIT = 60
_SEARCH_RATE_WINDOW = 60


@router.get("/{show_id}/my-ticket", response_model=TicketResponse, summary="Мой билет (QR)")
async def my_ticket(
    show_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return await svc.get_ticket(db, show_id, user)


@router.post("/{show_id}/checkin/scan", response_model=ParticipantCard, summary="Скан QR на стойке")
async def scan(
    show_id: uuid.UUID,
    body: ScanRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return await svc.scan(db, show_id, user, is_admin(user), body.token)


@router.get("/{show_id}/checkin/search", response_model=list[EntryCard], summary="Поиск на стойке")
async def search(
    show_id: uuid.UUID,
    request: Request,
    q: str = Query(..., min_length=1, max_length=64),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    user: User = Depends(get_current_user),
):
    # Перебор ПДн (телефоны, чипы) — ограничиваем частоту.
    await check_rate_limit(request, _SEARCH_RATE_LIMIT, _SEARCH_RATE_WINDOW, redis)
    return await svc.search(db, show_id, user, is_admin(user), q)


@router.post(
    "/{show_id}/entries/{entry_id}/checks",
    response_model=EntryCard,
    summary="Отметки по записи (пачкой, атомарно)",
)
async def add_checks(
    show_id: uuid.UUID,
    entry_id: uuid.UUID,
    body: EntryChecksCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return await svc.add_checks(db, show_id, entry_id, user, is_admin(user), body.checks)


@router.get(
    "/{show_id}/entries/{entry_id}/checks",
    response_model=list[EntryCheckResponse],
    summary="История отметок записи",
)
async def list_checks(
    show_id: uuid.UUID,
    entry_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return await svc.list_entry_checks(db, show_id, entry_id, user, is_admin(user))


@router.get("/{show_id}/checkin/summary", response_model=CheckinSummary, summary="Сводка стойки")
async def summary(
    show_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return await svc.summary(db, show_id, user, is_admin(user))


@router.get(
    "/{show_id}/checkin/precheck-queue",
    response_model=list[EntryCard],
    summary="Очередь предпроверки документов",
)
async def precheck_queue(
    show_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return await svc.precheck_queue(db, show_id, user, is_admin(user))
