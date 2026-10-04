"""Запросы чек-ина: документы собак, персонал, записи и отметки."""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import delete, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.dog import DogDocument
from app.models.file import UploadedFile
from app.models.show import Show, ShowEntry, ShowStaff, ShowStatus
from app.models.user import User

# Документы доступны персоналу, пока выставка «живая».
_ACTIVE_SHOW_STATUSES = (
    ShowStatus.draft,
    ShowStatus.registration_open,
    ShowStatus.registration_closed,
    ShowStatus.in_progress,
)


async def list_dog_documents(
    db: AsyncSession, dog_ids: Iterable[uuid.UUID]
) -> list[tuple[DogDocument, UploadedFile]]:
    ids = list(dog_ids)
    if not ids:
        return []
    stmt = (
        select(DogDocument, UploadedFile)
        .join(UploadedFile, UploadedFile.id == DogDocument.file_id)
        .where(DogDocument.dog_id.in_(ids))
        .order_by(DogDocument.created_at.desc())
    )
    return [(d, f) for d, f in (await db.execute(stmt)).all()]


async def get_dog_document(
    db: AsyncSession, dog_id: uuid.UUID, doc_id: uuid.UUID
) -> tuple[DogDocument, UploadedFile] | None:
    stmt = (
        select(DogDocument, UploadedFile)
        .join(UploadedFile, UploadedFile.id == DogDocument.file_id)
        .where(DogDocument.id == doc_id, DogDocument.dog_id == dog_id)
    )
    row = (await db.execute(stmt)).first()
    return (row[0], row[1]) if row else None


async def user_has_show_access_to_dog(
    db: AsyncSession, dog_id: uuid.UUID, user_id: uuid.UUID
) -> bool:
    """Организатор или персонал активной выставки, где у собаки есть запись."""
    staff = exists().where(ShowStaff.show_id == Show.id, ShowStaff.user_id == user_id)
    stmt = (
        select(ShowEntry.id)
        .join(Show, Show.id == ShowEntry.show_id)
        .where(
            ShowEntry.dog_id == dog_id,
            Show.status.in_(_ACTIVE_SHOW_STATUSES),
            or_(Show.organizer_id == user_id, staff),
        )
        .limit(1)
    )
    return (await db.execute(stmt)).first() is not None


async def is_staff(db: AsyncSession, show_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    stmt = select(ShowStaff.id).where(
        ShowStaff.show_id == show_id, ShowStaff.user_id == user_id
    )
    return (await db.execute(stmt)).first() is not None


async def list_staff(db: AsyncSession, show_id: uuid.UUID) -> list[tuple[ShowStaff, User]]:
    stmt = (
        select(ShowStaff, User)
        .join(User, User.id == ShowStaff.user_id)
        .options(selectinload(User.profile))
        .where(ShowStaff.show_id == show_id)
        .order_by(ShowStaff.created_at)
    )
    return [(s, u) for s, u in (await db.execute(stmt)).all()]


async def delete_staff(db: AsyncSession, show_id: uuid.UUID, user_id: uuid.UUID) -> int:
    res = await db.execute(
        delete(ShowStaff).where(ShowStaff.show_id == show_id, ShowStaff.user_id == user_id)
    )
    return getattr(res, "rowcount", 0) or 0


async def list_staffed_shows(db: AsyncSession, user_id: uuid.UUID) -> list[Show]:
    stmt = (
        select(Show)
        .join(ShowStaff, ShowStaff.show_id == Show.id)
        .where(ShowStaff.user_id == user_id, Show.status != ShowStatus.cancelled)
        .order_by(Show.date_start.desc())
    )
    return list((await db.execute(stmt)).scalars().unique())


async def get_user_by_email_ci(db: AsyncSession, email: str) -> User | None:
    """
    Поиск по email без учёта регистра: организатор вводит адрес регистратора
    вручную, а при регистрации email не нормализуется (users.email хранится
    как ввели).
    """
    stmt = select(User).where(func.lower(User.email) == email.strip().lower()).limit(1)
    return (await db.execute(stmt)).scalar_one_or_none()
