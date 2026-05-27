"""
Transactional outbox для надёжной публикации событий (follow-up для
этапов 9, 10).

Проблема fire-and-forget publish:
- publish_event/Rabbit publish может упасть (брокер недоступен,
  сетевой сбой), и событие потеряется. На стороне БД операция уже
  закоммичена, а подписчики никогда не узнают.

Решение — outbox:
1. В той же транзакции, что и бизнес-операция, делаем INSERT в
   `outbox_events`. Поскольку транзакционно — событие появится тогда
   и только тогда, когда операция действительно прошла.
2. Отдельный воркер (worker --mode outbox) опрашивает таблицу,
   publish'ит pending в RabbitMQ, помечает sent.
3. На сбое publish — оставляем pending; следующий тик подберёт.

Реализация в этом проекте:
- Событие = (event_type, routing_key, payload). routing_key используется
  для topic-exchange (см. notification.publish_event).
- Тип очереди определяется через `exchange` (None = direct
  default-exchange + routing_key как queue_name, иначе — topic-exchange
  с routing_key).
- attempts — счётчик попыток; через N (TODO) можно помечать failed.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Enum as SAEnum,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class OutboxStatus(str, enum.Enum):
    pending = "pending"
    sent = "sent"
    failed = "failed"


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        # Композитный индекс под "забери N pending старше всех" — основной
        # запрос воркера-диспатчера.
        Index(
            "ix_outbox_pending_created",
            "status",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # exchange=NULL означает default exchange + routing_key как имя
    # очереди (для прямой публикации в queue). Иначе routing_key
    # интерпретируется в контексте этого exchange'а (topic).
    exchange: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    routing_key: Mapped[str] = mapped_column(String(255))
    # Полное тело сообщения. JSONB — для гибкости и возможности фильтрации
    # из dashboard (например, "сколько litter.announced событий за день").
    payload: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[OutboxStatus] = mapped_column(
        SAEnum(OutboxStatus, name="outboxstatus"),
        default=OutboxStatus.pending,
        index=True,
    )
    attempts: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    last_error: Mapped[str | None] = mapped_column(
        String(2000), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
