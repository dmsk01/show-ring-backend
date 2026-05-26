"""
Audit-лог модерационных действий (этап 14 follow-up).

Зачем отдельная таблица:
- Решения модератора (одобрение/блокировка/смена роли) могут быть
  оспорены — нужен лог "кто, когда, что, причина".
- Уведомления автору контента (на этапе 15+) могут читать reason
  из этой таблицы, чтобы показать в /notifications.

Минимальная схема: actor (модератор) + action (тип решения) +
target_type/target_id (на что) + reason (свободный текст).
JSONB extra под детали конкретных действий (старый/новый статус и т.п.).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ModerationLog(Base):
    __tablename__ = "moderation_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # actor — кто сделал. SET NULL: если модератор уволился, лог
    # остаётся читаемым (action+reason важнее имени).
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # action — короткий код типа решения. Строка, не enum, чтобы
    # добавление новых действий не требовало миграции:
    #   classified.approve, classified.reject,
    #   kennel.verify, kennel.unverify,
    #   user.block, user.unblock,
    #   user.role_grant, user.role_revoke
    action: Mapped[str] = mapped_column(String(64), index=True)
    # На что подействовали. target_type — таблица ("classified"/"kennel"/"user"),
    # target_id — её PK. Полиморфизм без FK — потому что FK к разным
    # таблицам в одной колонке невозможен; лог — append-only, целостность
    # не критична как у бизнес-связей.
    target_type: Mapped[str] = mapped_column(String(32), index=True)
    target_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), index=True
    )
    # Свободный текст с причиной модератора. Видим автору контента.
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Дополнительные детали: {"prev_status": "active", "new_status": "closed"}
    # и т.п. Полезно для дебага и future "rollback" возможности.
    extra: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )
