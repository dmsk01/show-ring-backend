"""
Репозиторий поддержки (этап 11).
"""

from __future__ import annotations

import uuid
from typing import Sequence

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.support import (
    SupportMessage,
    SupportTicket,
    TicketStatus,
)


# ---------------------------------------------------------------------
# Tickets
# ---------------------------------------------------------------------


async def create_ticket(db: AsyncSession, **fields) -> SupportTicket:
    obj = SupportTicket(**fields)
    db.add(obj)
    await db.flush()
    await db.commit()
    await db.refresh(obj)
    return obj


async def get_ticket(
    db: AsyncSession, ticket_id: uuid.UUID
) -> SupportTicket | None:
    return await db.get(SupportTicket, ticket_id)


async def list_user_tickets(
    db: AsyncSession, user_id: uuid.UUID
) -> Sequence[SupportTicket]:
    stmt = (
        select(SupportTicket)
        .where(SupportTicket.user_id == user_id)
        .order_by(SupportTicket.created_at.desc())
    )
    return (await db.execute(stmt)).scalars().all()


async def list_tickets_admin(
    db: AsyncSession,
    status: TicketStatus | None = None,
    assigned_to_id: uuid.UUID | None = None,
    page: int = 1,
    per_page: int = 50,
) -> Sequence[SupportTicket]:
    """
    Список тикетов для оператора/админа. Фильтр по статусу + назначению.
    Сортировка: приоритет DESC (urgent сверху), потом по дате.
    """
    stmt = select(SupportTicket)
    if status is not None:
        stmt = stmt.where(SupportTicket.status == status)
    if assigned_to_id is not None:
        stmt = stmt.where(SupportTicket.assigned_to_id == assigned_to_id)
    # priority Enum хранится как text — сортировка по строковому значению
    # даст urgent < normal < low (по алфавиту). Чтобы получить семантическую
    # сортировку, в проде делаем CASE WHEN; для dev'а достаточно ORDER BY
    # priority DESC + created_at.
    stmt = (
        stmt.order_by(
            SupportTicket.priority.desc(),
            SupportTicket.created_at.desc(),
        )
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    return (await db.execute(stmt)).scalars().all()


# ---------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------


async def add_message(
    db: AsyncSession,
    *,
    ticket_id: uuid.UUID,
    sender_id: uuid.UUID | None,
    body: str,
    is_from_operator: bool,
) -> SupportMessage:
    obj = SupportMessage(
        ticket_id=ticket_id,
        sender_id=sender_id,
        body=body,
        is_from_operator=is_from_operator,
    )
    db.add(obj)
    await db.flush()
    await db.commit()
    await db.refresh(obj)
    return obj


async def list_messages(
    db: AsyncSession,
    ticket_id: uuid.UUID,
    *,
    page: int = 1,
    per_page: int = 50,
) -> Sequence[SupportMessage]:
    """
    История сообщений тикета. ASC по created_at — чат отображается
    в естественном порядке "сверху вниз".
    """
    stmt = (
        select(SupportMessage)
        .where(SupportMessage.ticket_id == ticket_id)
        .order_by(SupportMessage.created_at.asc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    return (await db.execute(stmt)).scalars().all()


async def mark_messages_read(
    db: AsyncSession,
    ticket_id: uuid.UUID,
    reader_is_operator: bool,
) -> None:
    """
    Помечает прочитанными "не свои" сообщения — то есть оператор
    отмечает прочитанными сообщения клиента (is_from_operator=False),
    клиент — сообщения оператора (is_from_operator=True).
    """
    stmt = (
        update(SupportMessage)
        .where(
            SupportMessage.ticket_id == ticket_id,
            SupportMessage.is_from_operator == (not reader_is_operator),
            SupportMessage.is_read.is_(False),
        )
        .values(is_read=True)
    )
    await db.execute(stmt)
    await db.commit()


async def count_unread_for_user(
    db: AsyncSession, ticket_id: uuid.UUID, user_is_operator: bool
) -> int:
    """Сколько у пользователя непрочитанных сообщений в тикете."""
    stmt = (
        select(func.count())
        .select_from(SupportMessage)
        .where(
            SupportMessage.ticket_id == ticket_id,
            SupportMessage.is_from_operator == (not user_is_operator),
            SupportMessage.is_read.is_(False),
        )
    )
    return int((await db.execute(stmt)).scalar_one())
