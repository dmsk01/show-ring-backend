"""
Роутер чек-ина (регистрация прибытия на выставку).

Префикс /shows, как у shows.py, но отдельным модулем: shows.py уже
~600 строк. Пути /shows/staff/my и /shows/{id}/... не пересекаются с
маршрутами shows.py (у тех нет второго сегмента staff/checkin/my-ticket).
"""

from __future__ import annotations

import uuid
from typing import NoReturn

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user, is_admin
from app.models.user import User
from app.schemas.checkin import CheckinSettingsUpdate, ShowStaffAdd, ShowStaffResponse
from app.schemas.show import ShowResponse
from app.services import checkin as svc

router = APIRouter(prefix="/shows", tags=["checkin"])

_NOT_FOUND = {
    "not_found", "entry_not_found", "user_not_found", "staff_not_found",
    "document_not_found", "token_other_show", "no_entries",
}
_CONFLICT = {"checkin_disabled", "invalid_show_status", "already_staff", "show_locked"}


def _raise_for_error(err: ValueError) -> NoReturn:
    code = str(err)
    if code in _NOT_FOUND:
        raise HTTPException(404, code)
    if code == "forbidden":
        raise HTTPException(403, code)
    if code in _CONFLICT:
        raise HTTPException(409, code)
    if code == "document_mismatch":
        raise HTTPException(422, code)
    raise HTTPException(400, code)


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
    try:
        return await svc.set_enabled(db, show_id, user.id, is_admin(user), body.enabled)
    except ValueError as e:
        _raise_for_error(e)


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
    try:
        return await svc.list_staff(db, show_id, user.id, is_admin(user))
    except ValueError as e:
        _raise_for_error(e)


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
    try:
        return await svc.add_staff(
            db, show_id, user.id, is_admin(user),
            email=str(body.email) if body.email else None, phone=body.phone,
        )
    except ValueError as e:
        _raise_for_error(e)


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
    try:
        await svc.remove_staff(db, show_id, user.id, is_admin(user), user_id)
    except ValueError as e:
        _raise_for_error(e)
