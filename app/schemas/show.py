"""
Схемы выставок (этап 6).

Pydantic-схемы для Show и связанных сущностей. Разделяем Create/Update/Response
для каждой сущности по тому же принципу, что и в остальных модулях.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.show import ShowStatus


# ---------------------------------------------------------------------
# Show
# ---------------------------------------------------------------------


class ShowBase(BaseModel):
    name: str = Field(..., max_length=255)
    rank_id: uuid.UUID
    description: str | None = None
    date_start: date
    date_end: date | None = None
    city: str | None = Field(None, max_length=128)
    country: str | None = Field(None, max_length=64)
    venue: str | None = Field(None, max_length=255)
    entry_fee: Decimal | None = Field(None, ge=0)
    registration_deadline: date | None = None

    @model_validator(mode="after")
    def _validate_dates(self) -> "ShowBase":
        # date_end не раньше date_start.
        if self.date_end is not None and self.date_end < self.date_start:
            raise ValueError("date_end must be >= date_start")
        # Дедлайн регистрации не позже даты начала выставки.
        if (
            self.registration_deadline is not None
            and self.registration_deadline > self.date_start
        ):
            raise ValueError(
                "registration_deadline must be <= date_start"
            )
        return self


class ShowCreate(ShowBase):
    # Allow-list пород при создании. Пустой список = всепородная выставка.
    breed_ids: list[uuid.UUID] = Field(default_factory=list)


class ShowUpdate(BaseModel):
    name: str | None = Field(None, max_length=255)
    description: str | None = None
    date_start: date | None = None
    date_end: date | None = None
    city: str | None = Field(None, max_length=128)
    country: str | None = Field(None, max_length=64)
    venue: str | None = Field(None, max_length=255)
    entry_fee: Decimal | None = Field(None, ge=0)
    registration_deadline: date | None = None


class ShowStatusUpdate(BaseModel):
    status: ShowStatus


class ShowResponse(ShowBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organizer_id: uuid.UUID
    status: ShowStatus
    created_at: datetime
    updated_at: datetime


class ShowPage(BaseModel):
    items: list[ShowResponse]
    total: int
    page: int
    per_page: int


# ---------------------------------------------------------------------
# Judges
# ---------------------------------------------------------------------


class ShowJudgeCreate(BaseModel):
    judge_id: uuid.UUID
    # Ровно одно из двух: порода ИЛИ группа. Валидируется на уровне модели
    # и сервиса; на схеме делаем soft-проверку для дружелюбной ошибки.
    breed_id: uuid.UUID | None = None
    breed_group_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _xor_target(self) -> "ShowJudgeCreate":
        a = self.breed_id is not None
        b = self.breed_group_id is not None
        if a == b:
            # XOR: обе заданы или ни одна.
            raise ValueError(
                "Provide exactly one of breed_id or breed_group_id"
            )
        return self


class ShowJudgeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    show_id: uuid.UUID
    judge_id: uuid.UUID
    breed_id: uuid.UUID | None
    breed_group_id: uuid.UUID | None


# ---------------------------------------------------------------------
# Rings
# ---------------------------------------------------------------------


class ShowRingCreate(BaseModel):
    ring_number: int = Field(..., ge=1)
    breed_id: uuid.UUID | None = None
    breed_group_id: uuid.UUID | None = None
    show_class_id: uuid.UUID | None = None
    judge_id: uuid.UUID | None = None
    ring_date: date | None = None
    time_start: time | None = None
    time_end: time | None = None
    location: str | None = Field(None, max_length=255)

    @model_validator(mode="after")
    def _validate_times(self) -> "ShowRingCreate":
        if (
            self.time_start is not None
            and self.time_end is not None
            and self.time_end <= self.time_start
        ):
            raise ValueError("time_end must be > time_start")
        return self


class ShowRingUpdate(BaseModel):
    ring_number: int | None = Field(None, ge=1)
    breed_id: uuid.UUID | None = None
    breed_group_id: uuid.UUID | None = None
    show_class_id: uuid.UUID | None = None
    judge_id: uuid.UUID | None = None
    ring_date: date | None = None
    time_start: time | None = None
    time_end: time | None = None
    location: str | None = Field(None, max_length=255)


class ShowRingResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    show_id: uuid.UUID
    ring_number: int
    breed_id: uuid.UUID | None
    breed_group_id: uuid.UUID | None
    show_class_id: uuid.UUID | None
    judge_id: uuid.UUID | None
    ring_date: date | None
    time_start: time | None
    time_end: time | None
    location: str | None


# ---------------------------------------------------------------------
# Entries
# ---------------------------------------------------------------------


class ShowEntryCreate(BaseModel):
    dog_id: uuid.UUID
    # Класс выбирает владелец из списка доступных
    # (см. AvailableClassesResponse). Не вычисляется автоматически.
    show_class_id: uuid.UUID
    handler_id: uuid.UUID | None = None
    notes: str | None = None


class ShowEntryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    show_id: uuid.UUID
    dog_id: uuid.UUID
    show_class_id: uuid.UUID
    handler_id: uuid.UUID | None
    registered_by: uuid.UUID
    catalog_number: int | None
    notes: str | None
    created_at: datetime


class ShowEntryPage(BaseModel):
    items: list[ShowEntryResponse]
    total: int
    page: int
    per_page: int


# ---------------------------------------------------------------------
# Available classes (для выбора владельцем)
# ---------------------------------------------------------------------


class AvailableClass(BaseModel):
    """
    Один доступный класс для собаки на конкретной выставке.

    requires_documents=True означает, что класс по возрасту доступен,
    но требует подтверждающих документов (рабочий сертификат — рабочий
    класс; титул чемпиона — класс чемпионов). На этапе 6 это просто
    флаг; жёсткая валидация документов делается в этапе 7.
    """

    id: uuid.UUID
    code: str
    name: str
    age_from_months: int
    age_to_months: int | None
    can_receive_cac: bool
    requires_documents: bool = False
    documents_note: str | None = None


class AvailableClassesResponse(BaseModel):
    dog_id: uuid.UUID
    age_at_show_months: int
    classes: list[AvailableClass]
