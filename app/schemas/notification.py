"""
Схемы подписок и уведомлений (этап 9).

EventMessage — Pydantic-модель сообщения, которое летит через RabbitMQ
topic exchange. Не привязана к ORM, чтобы воркер мог парсить event
без обращения к БД.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field

from app.models.notification import (
    EventType,
    NotificationChannel,
    NotificationStatus,
)


# ---------------------------------------------------------------------
# Subscription
# ---------------------------------------------------------------------


class SubscriptionCreate(BaseModel):
    """
    Создание подписки. user_id не принимаем — берётся из current_user
    в роутере (нельзя подписываться "за кого-то").

    event_type валидируем по EventType — иначе пользователь мог бы
    создать подписку на любую строку, которую воркер всё равно не
    будет матчить.
    """

    event_type: EventType
    filter_breed_id: uuid.UUID | None = None
    filter_region: str | None = Field(None, max_length=128)
    channel: NotificationChannel = NotificationChannel.email


class SubscriptionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    event_type: str
    filter_breed_id: uuid.UUID | None
    filter_region: str | None
    channel: NotificationChannel
    is_active: bool
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------
# Notification (лог)
# ---------------------------------------------------------------------


class NotificationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    event_type: str
    channel: NotificationChannel
    subject: str
    status: NotificationStatus
    error: str | None
    sent_at: datetime | None
    # read_at — момент прочтения в UI (NULL = непрочитано). status выше
    # про доставку email, не про прочтение — не путать.
    read_at: datetime | None
    created_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_read(self) -> bool:
        """Удобный флаг для фронта: прочитано ли (read_at проставлен)."""
        return self.read_at is not None


# ---------------------------------------------------------------------
# Event сообщения (внутренний контракт между сервисами через RabbitMQ)
# ---------------------------------------------------------------------


class EventMessage(BaseModel):
    """
    Сообщение события для topic exchange.

    routing_key собирается из event_type + дополнительных полей. Пример:
    - "show.registration_opened"
    - "litter.announced.breed.{breed_id}"

    payload — гибкий dict с данными события. Воркер событий читает
    payload, формирует subject/body email'а через Jinja2-шаблон.
    """

    # bug_230 audit 2026-05-28: event_id — идемпотентный ключ события.
    # Генерится в момент публикации; при retry/redelivery остаётся тем
    # же (он лежит в payload, который не меняется). events_handler
    # использует его как часть per-recipient message_id, чтобы
    # повторная обработка того же event не плодила дубли Notification.
    event_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    event_type: str
    routing_key: str
    payload: dict[str, Any] = Field(default_factory=dict)
    # actor_id — кто инициировал событие. Полезно при дебаге; не отправляем
    # ему же уведомление о собственном действии (см. notification.service).
    actor_id: uuid.UUID | None = None
    # Сам ивент-таймстемп — нужен в шаблонах писем и для дебага.
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, data: str | bytes) -> "EventMessage":
        if isinstance(data, bytes):
            data = data.decode()
        return cls.model_validate_json(data)


# ---------------------------------------------------------------------
# Email task message (внутренний контракт voor email worker)
# ---------------------------------------------------------------------


class EmailTaskMessage(BaseModel):
    """
    Сообщение для воркера email_tasks. Содержит уже готовое письмо
    (subject + html_body) и ID уведомления в БД — воркер обновит
    его статус после отправки.
    """

    notification_id: uuid.UUID
    # bug_230 audit 2026-05-28: per-recipient идемпотентный ключ.
    # Должен совпадать с Notification.message_id. email_handler
    # дополнительно проверяет статус уведомления и не отправляет
    # дубликат, если RabbitMQ повторно доставил task.
    message_id: uuid.UUID
    to_email: str
    subject: str
    html_body: str
    # text_body — fallback для клиентов без HTML. Заполняется простым
    # текстом из шаблона.
    text_body: str | None = None

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, data: str | bytes) -> "EmailTaskMessage":
        if isinstance(data, bytes):
            data = data.decode()
        return cls.model_validate_json(data)
