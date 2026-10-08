"""Запросы чек-ина: документы собак, персонал, записи и отметки."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import datetime, timezone

from sqlalchemy import delete, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.dog import Dog, DogDocument, DogPhoto
from app.models.file import UploadedFile
from app.models.reference import ShowClass
from app.models.show import (
    AttendanceStatus,
    EntryCheck,
    EntryCheckKind,
    Show,
    ShowEntry,
    ShowStaff,
    ShowStatus,
)
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


async def list_participant_entries(
    db: AsyncSession, show_id: uuid.UUID, user_id: uuid.UUID
) -> list[ShowEntry]:
    """Записи, где человек — записавший, хендлер или владелец собаки."""
    stmt = (
        select(ShowEntry)
        .join(Dog, Dog.id == ShowEntry.dog_id)
        .where(
            ShowEntry.show_id == show_id,
            or_(
                ShowEntry.registered_by == user_id,
                ShowEntry.handler_id == user_id,
                Dog.owner_id == user_id,
            ),
        )
        .order_by(ShowEntry.catalog_number.asc().nulls_last(), ShowEntry.created_at)
    )
    return list((await db.execute(stmt)).scalars().unique())


async def search_entries(
    db: AsyncSession, show_id: uuid.UUID, q: str, limit: int = 20
) -> list[ShowEntry]:
    """
    Поиск на стойке. Цифры (до 6) — номер каталога; «+…» — телефон
    записавшего; иначе — точный чип/клеймо или подстрока клички.
    """
    q = q.strip()
    stmt = (
        select(ShowEntry)
        .join(Dog, Dog.id == ShowEntry.dog_id)
        .where(ShowEntry.show_id == show_id)
    )
    if q.isdigit() and len(q) <= 6:
        stmt = stmt.where(
            or_(ShowEntry.catalog_number == int(q), Dog.microchip == q, Dog.tattoo == q)
        )
    elif q.startswith("+"):
        stmt = stmt.join(User, User.id == ShowEntry.registered_by).where(User.phone == q)
    else:
        lowered = q.lower()
        stmt = stmt.where(
            or_(
                func.lower(Dog.microchip) == lowered,
                func.lower(Dog.tattoo) == lowered,
                Dog.name.ilike(f"%{q}%"),
            )
        )
    stmt = stmt.order_by(ShowEntry.catalog_number.asc().nulls_last()).limit(limit)
    return list((await db.execute(stmt)).scalars().unique())


async def get_entry_for_update(
    db: AsyncSession, show_id: uuid.UUID, entry_id: uuid.UUID
) -> ShowEntry | None:
    stmt = (
        select(ShowEntry)
        .where(ShowEntry.id == entry_id, ShowEntry.show_id == show_id)
        .with_for_update()
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def list_checks(db: AsyncSession, entry_ids: Iterable[uuid.UUID]) -> list[EntryCheck]:
    ids = list(entry_ids)
    if not ids:
        return []
    stmt = (
        select(EntryCheck)
        .where(EntryCheck.entry_id.in_(ids))
        .order_by(EntryCheck.created_at, EntryCheck.id)
    )
    return list((await db.execute(stmt)).scalars())


async def load_card_context(db: AsyncSession, entries: list[ShowEntry]):
    """Пакетная подгрузка всего, что нужно карточкам (без N+1)."""
    dog_ids = {e.dog_id for e in entries}
    class_ids = {e.show_class_id for e in entries}
    user_ids = {e.registered_by for e in entries}
    dogs = {d.id: d for d in (await db.execute(select(Dog).where(Dog.id.in_(dog_ids)))).scalars()}
    classes = {
        c.id: c for c in (await db.execute(select(ShowClass).where(ShowClass.id.in_(class_ids)))).scalars()
    }
    users = {
        u.id: u
        for u in (
            await db.execute(
                select(User).options(selectinload(User.profile)).where(User.id.in_(user_ids))
            )
        ).scalars()
    }
    photos = (
        await db.execute(
            select(DogPhoto)
            .where(DogPhoto.dog_id.in_(dog_ids))
            .order_by(DogPhoto.is_primary.desc(), DogPhoto.position)
        )
    ).scalars()
    avatars: dict[uuid.UUID, uuid.UUID] = {}
    for p in photos:
        avatars.setdefault(p.dog_id, p.file_id)
    docs = await list_dog_documents(db, dog_ids)
    checks = await list_checks(db, [e.id for e in entries])
    return dogs, classes, users, avatars, docs, checks


async def load_users(db: AsyncSession, user_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, User]:
    ids = [i for i in set(user_ids) if i is not None]
    if not ids:
        return {}
    stmt = select(User).options(selectinload(User.profile)).where(User.id.in_(ids))
    return {u.id: u for u in (await db.execute(stmt)).scalars()}


async def attendance_counts(db: AsyncSession, show_id: uuid.UUID) -> dict[AttendanceStatus, int]:
    stmt = (
        select(ShowEntry.attendance_status, func.count())
        .where(ShowEntry.show_id == show_id)
        .group_by(ShowEntry.attendance_status)
    )
    return {status: count for status, count in (await db.execute(stmt)).all()}


async def precheck_queue(db: AsyncSession, show_id: uuid.UUID, limit: int = 200) -> list[ShowEntry]:
    """
    Записи, у собак которых есть документы, но нет АКТУАЛЬНОЙ отметки
    docs_precheck. Отметка устаревает, если после неё загружен новый
    документ: владелец исправил скан после отказа — запись снова в очереди.
    """
    has_docs = exists().where(DogDocument.dog_id == ShowEntry.dog_id)
    latest_doc_at = (
        select(func.max(DogDocument.created_at))
        .where(DogDocument.dog_id == ShowEntry.dog_id)
        .scalar_subquery()
    )
    has_fresh_precheck = exists().where(
        EntryCheck.entry_id == ShowEntry.id,
        EntryCheck.kind == EntryCheckKind.docs_precheck,
        EntryCheck.created_at >= latest_doc_at,
    )
    stmt = (
        select(ShowEntry)
        .where(ShowEntry.show_id == show_id, has_docs, ~has_fresh_precheck)
        .order_by(ShowEntry.catalog_number.asc().nulls_last(), ShowEntry.created_at)
        .limit(limit)
    )
    return list((await db.execute(stmt)).scalars())


async def mark_registered_absent(db: AsyncSession, show_id: uuid.UUID) -> int:
    """При старте выставки: все ещё не отмеченные записи → absent."""
    res = await db.execute(
        update(ShowEntry)
        .where(
            ShowEntry.show_id == show_id,
            ShowEntry.attendance_status == AttendanceStatus.registered,
        )
        .values(
            attendance_status=AttendanceStatus.absent,
            attendance_changed_at=datetime.now(timezone.utc),
        )
    )
    return getattr(res, "rowcount", 0) or 0
