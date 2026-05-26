"""
Модели онлайн-поддержки (этап 11).

Два уровня:
1. SupportTicket — обращение пользователя. Статус-машина:
   open → in_progress → resolved → closed.
2. SupportMessage — сообщения внутри тикета. is_from_operator
   определяет визуальную сторону рендера (слева/справа).

Решения:
- assigned_to_id отдельной колонкой, а не через FK на отдельную таблицу
  ticket_operators — у тикета один оператор за раз; переназначение
  сводится к UPDATE одного поля.
- is_read хранится на сообщении, а не "last_read_at" у пользователя —
  это даёт возможность позже "пометить непрочитанным" одно сообщение,
  показать badge с точным числом.
- priority как Enum: low/normal/high/urgent. Помогает оператору
  сортировать очередь.
"""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import (
    Boolean,
    Enum as SAEnum,
    ForeignKey,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class TicketStatus(str, enum.Enum):
    open = "open"
    in_progress = "in_progress"
    resolved = "resolved"
    closed = "closed"


class TicketPriority(str, enum.Enum):
    low = "low"
    normal = "normal"
    high = "high"
    urgent = "urgent"


class SupportTicket(Base, TimestampMixin):
    __tablename__ = "support_tickets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        # CASCADE — тикеты исчезают вместе с автором; история обращений
        # без пользователя не имеет смысла (на исторические цели
        # достаточно audit_log в будущем).
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    subject: Mapped[str] = mapped_column(String(255))
    status: Mapped[TicketStatus] = mapped_column(
        SAEnum(TicketStatus, name="ticketstatus"),
        default=TicketStatus.open,
        index=True,
    )
    priority: Mapped[TicketPriority] = mapped_column(
        SAEnum(TicketPriority, name="ticketpriority"),
        default=TicketPriority.normal,
        index=True,
    )
    # Назначенный оператор. SET NULL — если оператор уволился,
    # тикет остаётся "висеть" без назначения; admin переназначит.
    assigned_to_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    messages: Mapped[list["SupportMessage"]] = relationship(
        back_populates="ticket",
        cascade="all, delete-orphan",
        order_by="SupportMessage.created_at",
    )


class SupportMessage(Base, TimestampMixin):
    __tablename__ = "support_messages"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("support_tickets.id", ondelete="CASCADE"),
        index=True,
    )
    sender_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        # SET NULL — если автор сообщения удалён (например, deleted
        # account), сама переписка остаётся читаемой.
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Денормализация: вместо JOIN на user_roles, чтобы понять "оператор
    # это или клиент", прокидываем флаг в момент INSERT'а. Так фронт
    # рендерит чат без второго запроса.
    is_from_operator: Mapped[bool] = mapped_column(Boolean, default=False)
    body: Mapped[str] = mapped_column(Text)
    # is_read у получателя. Если is_from_operator=True — флаг "прочитал
    # клиент"; если False — "прочитал оператор". Семантика "не моё
    # сообщение".
    is_read: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", index=True
    )

    ticket: Mapped["SupportTicket"] = relationship(back_populates="messages")
