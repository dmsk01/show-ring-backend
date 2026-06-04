"""
Аудит-лог чувствительных операций пользователя над собственным
аккаунтом (этап 19).

Отдельная таблица от moderation_logs: там actor — модератор над ЧУЖИМ
контентом (action='user.block' и т.п.), здесь actor = САМ пользователь
над собой (сменил email/пароль). Нужны ip/user_agent для расследования
угонов — в moderation_logs их нет.

append-only: записи не обновляются и не удаляются (cleanup старых —
отдельная cron-задача в будущем).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class SecurityAuditLog(Base):
    __tablename__ = "security_audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # SET NULL: если юзер удалён, лог инцидента остаётся читаемым.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # action — короткий код. Строка, не enum: новые типы без миграции.
    #   email_change_requested, email_change_confirmed, password_changed
    action: Mapped[str] = mapped_column(String(64), index=True)
    # ip/user_agent берутся из Request в роутере и прокидываются в сервис.
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # extra — детали: {"old_email": "...", "new_email": "..."} и т.п.
    extra: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )
