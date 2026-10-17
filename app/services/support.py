"""
Сервис поддержки (этап 11).

Бизнес-правила:
- Создавать тикет может любой authenticated.
- Видеть/писать в тикет могут: автор, назначенный оператор, admin
  и любой пользователь с ролью operator (если тикет не назначен).
- Менять статус — только operator / admin.
- Назначать оператора — только admin.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import Forbidden, NotFound
from app.models.support import (
    SupportMessage,
    SupportTicket,
    TicketPriority,
    TicketStatus,
)
from app.models.user import User
from app.repositories import support as repo
from app import policies
from app.policies import Actor


def is_operator(user: User) -> bool:
    """Правило — в app/policies.py (BE-33); здесь обёртка для кода с User."""
    return policies.is_operator(Actor.of(user))


def can_access_ticket(ticket: SupportTicket, user: User) -> bool:
    """Правило — в app/policies.py (BE-33); здесь обёртка для кода с User."""
    return policies.can_access_ticket(ticket, Actor.of(user))


async def create_ticket(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    subject: str,
    body: str,
    priority: TicketPriority,
) -> tuple[SupportTicket, SupportMessage]:
    """
    Создаёт тикет и первое сообщение от пользователя в одной операции
    (атомарность: либо оба, либо ни одного).
    """
    ticket = await repo.create_ticket(
        db,
        user_id=user_id,
        subject=subject,
        priority=priority,
        status=TicketStatus.open,
    )
    message = await repo.add_message(
        db,
        ticket_id=ticket.id,
        sender_id=user_id,
        body=body,
        is_from_operator=False,
    )
    return ticket, message


async def change_status(
    db: AsyncSession,
    ticket_id: uuid.UUID,
    user: User,
    target: TicketStatus,
) -> SupportTicket:
    obj = await repo.get_ticket(db, ticket_id)
    if obj is None:
        raise NotFound("not_found")
    if not is_operator(user):
        raise Forbidden("forbidden")
    obj.status = target
    await db.commit()
    await db.refresh(obj)
    return obj


async def assign_operator(
    db: AsyncSession,
    ticket_id: uuid.UUID,
    user: User,
    operator_id: uuid.UUID | None,
) -> SupportTicket:
    obj = await repo.get_ticket(db, ticket_id)
    if obj is None:
        raise NotFound("not_found")
    # Только admin может назначать.
    if not any(r.role.value == "admin" for r in user.roles):
        raise Forbidden("forbidden")
    obj.assigned_to_id = operator_id
    # Назначение оператора автоматически переводит тикет в in_progress
    # (если был open). Это удобно — оператор не забудет вручную.
    if operator_id is not None and obj.status == TicketStatus.open:
        obj.status = TicketStatus.in_progress
    await db.commit()
    await db.refresh(obj)
    return obj


async def post_message(
    db: AsyncSession,
    ticket_id: uuid.UUID,
    user: User,
    body: str,
) -> SupportMessage:
    """
    REST-fallback для отправки сообщения. WebSocket-ветка делает то же
    через ws_manager.broadcast_message.
    """
    ticket = await repo.get_ticket(db, ticket_id)
    if ticket is None:
        raise NotFound("not_found")
    if not can_access_ticket(ticket, user):
        raise Forbidden("forbidden")
    msg = await repo.add_message(
        db,
        ticket_id=ticket_id,
        sender_id=user.id,
        body=body,
        is_from_operator=is_operator(user),
    )
    return msg
