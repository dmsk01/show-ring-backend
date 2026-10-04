"""
Сервис чек-ина: флаг выставки, персонал, билет участника, стойка
(скан/поиск/отметки), сводка и очередь предпроверки.

Ошибки — ValueError("code"), маппинг в HTTP — routers/checkin.py.
"""

from __future__ import annotations

import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.show import Show, ShowStaff, ShowStaffRole, ShowStatus
from app.models.user import User
from app.repositories import checkin as repo
from app.repositories import show as show_repo
from app.repositories import user as user_repo
from app.schemas.checkin import ShowStaffResponse
from app.utils.names import full_name


def display_name(user: User | None) -> str:
    """ФИО → email → телефон: на стойке нужно хоть как-то назвать человека."""
    if user is None:
        return "—"
    return full_name(user) or user.phone or "—"


async def get_show(db: AsyncSession, show_id: uuid.UUID) -> Show:
    show = await show_repo.get_show(db, show_id)
    if show is None:
        raise ValueError("not_found")
    return show


def ensure_organizer(show: Show, user_id: uuid.UUID, is_admin: bool) -> None:
    if not is_admin and show.organizer_id != user_id:
        raise ValueError("forbidden")


async def ensure_desk_access(
    db: AsyncSession, show: Show, user_id: uuid.UUID, is_admin: bool
) -> None:
    if is_admin or show.organizer_id == user_id:
        return
    if await repo.is_staff(db, show.id, user_id):
        return
    raise ValueError("forbidden")


def ensure_enabled(show: Show) -> None:
    if not show.checkin_enabled:
        raise ValueError("checkin_disabled")


async def set_enabled(
    db: AsyncSession, show_id: uuid.UUID, user_id: uuid.UUID, is_admin: bool, enabled: bool
) -> Show:
    show = await get_show(db, show_id)
    ensure_organizer(show, user_id, is_admin)
    if show.status in (ShowStatus.completed, ShowStatus.cancelled):
        raise ValueError("show_locked")
    show.checkin_enabled = enabled
    await db.commit()
    await db.refresh(show)
    return show


def _staff_response(staff: ShowStaff, user: User) -> ShowStaffResponse:
    return ShowStaffResponse(
        user_id=user.id, role=staff.role, display_name=display_name(user),
        email=user.email, phone=user.phone, created_at=staff.created_at,
    )


async def list_staff(
    db: AsyncSession, show_id: uuid.UUID, user_id: uuid.UUID, is_admin: bool
) -> list[ShowStaffResponse]:
    show = await get_show(db, show_id)
    ensure_organizer(show, user_id, is_admin)
    return [_staff_response(s, u) for s, u in await repo.list_staff(db, show_id)]


async def add_staff(
    db: AsyncSession,
    show_id: uuid.UUID,
    requester_id: uuid.UUID,
    is_admin: bool,
    *,
    email: str | None,
    phone: str | None,
) -> ShowStaffResponse:
    show = await get_show(db, show_id)
    ensure_organizer(show, requester_id, is_admin)
    if email is not None:
        user = await repo.get_user_by_email_ci(db, email)
    else:
        user = await user_repo.get_user_by_phone(db, phone or "")
    if user is None:
        raise ValueError("user_not_found")
    # Явная проверка — обычный путь; IntegrityError ниже — страховка от
    # гонки двух одновременных добавлений.
    if await repo.is_staff(db, show.id, user.id):
        raise ValueError("already_staff")
    staff = ShowStaff(
        show_id=show.id, user_id=user.id, role=ShowStaffRole.registrar,
        added_by=requester_id,
    )
    db.add(staff)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise ValueError("already_staff") from None
    await db.refresh(staff)
    for s, u in await repo.list_staff(db, show.id):
        if s.id == staff.id:
            return _staff_response(s, u)
    raise ValueError("not_found")  # недостижимо: строку только что вставили


async def remove_staff(
    db: AsyncSession, show_id: uuid.UUID, requester_id: uuid.UUID, is_admin: bool,
    user_id: uuid.UUID,
) -> None:
    show = await get_show(db, show_id)
    ensure_organizer(show, requester_id, is_admin)
    if await repo.delete_staff(db, show_id, user_id) == 0:
        raise ValueError("staff_not_found")
    await db.commit()


async def list_my_shows(db: AsyncSession, user_id: uuid.UUID) -> list[Show]:
    return await repo.list_staffed_shows(db, user_id)
