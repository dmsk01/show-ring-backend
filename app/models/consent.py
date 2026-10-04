"""
Журнал согласий пользователя (правовые документы, 152-ФЗ).

Зачем: обязанность доказать получение согласия лежит на операторе
(ч. 3 ст. 9 152-ФЗ). Строка = факт принятия конкретной РЕДАКЦИИ
документа в конкретный момент с конкретного IP/User-Agent.

append-mostly: выдача согласия — INSERT, отзыв — UPDATE revoked_at.
Строки не перезаписываются, поэтому история «принял редакцию A,
потом B, потом отозвал» восстанавливается целиком.

target_id — для согласий на распространение (ст. 10.1), которые даются
по конкретной публикации: питомник, объявление. Полиморфно без FK, как
moderation_logs.target_id: публикация может быть удалена, а след
согласия должен остаться до удаления аккаунта.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class UserConsent(Base):
    __tablename__ = "user_consents"
    __table_args__ = (
        # Горячий запрос: «активные согласия юзера по виду».
        Index("ix_user_consents_user_kind", "user_id", "kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # CASCADE: при удалении аккаунта обработка прекращена, хранить
    # доказательства согласия на неё дальше незачем.
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    # Строка, не enum: новый документ — без миграции (см. ConsentKind).
    kind: Mapped[str] = mapped_column(String(64))
    # Редакция документа, которую видел пользователь (app.services.consent).
    revision: Mapped[str] = mapped_column(String(32))
    target_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
