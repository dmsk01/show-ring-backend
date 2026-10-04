"""Схемы чек-ина и документов собаки."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from app.models.dog import DogDocumentKind
from app.models.show import (
    AttendanceStatus,
    EntryCheckKind,
    EntryCheckResult,
    ShowStaffRole,
)
from app.schemas.user import E164Phone


class DogDocumentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    dog_id: uuid.UUID
    kind: DogDocumentKind
    valid_until: date | None
    created_at: datetime
    original_filename: str
    content_type: str
    size_bytes: int
    # Действующий документ своего вида (последний загруженный).
    is_current: bool = False


class CheckinSettingsUpdate(BaseModel):
    enabled: bool


class ShowStaffAdd(BaseModel):
    """Ровно одно из полей: email или телефон в E.164."""

    email: EmailStr | None = None
    phone: E164Phone | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "ShowStaffAdd":
        if (self.email is None) == (self.phone is None):
            raise ValueError("Укажите email или телефон (ровно одно)")
        return self


class ShowStaffResponse(BaseModel):
    user_id: uuid.UUID
    role: ShowStaffRole
    display_name: str
    email: str | None
    phone: str | None
    created_at: datetime


class EntryCheckCreate(BaseModel):
    kind: EntryCheckKind
    result: EntryCheckResult
    comment: str | None = Field(None, max_length=1000)
    document_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _comment_on_failure(self) -> "EntryCheckCreate":
        if self.result == EntryCheckResult.failed and not (self.comment or "").strip():
            raise ValueError("Для отметки «не пройдено» нужен комментарий")
        return self


class EntryChecksCreate(BaseModel):
    checks: list[EntryCheckCreate] = Field(..., min_length=1, max_length=4)


class EntryCheckResponse(BaseModel):
    id: uuid.UUID
    kind: EntryCheckKind
    result: EntryCheckResult
    comment: str | None
    document_id: uuid.UUID | None
    performed_by: uuid.UUID | None
    performed_by_name: str | None
    created_at: datetime


class CardDog(BaseModel):
    id: uuid.UUID
    name: str
    microchip: str | None
    tattoo: str | None
    rkf_number: str | None
    avatar_file_id: uuid.UUID | None


class EntryCard(BaseModel):
    entry_id: uuid.UUID
    show_id: uuid.UUID
    catalog_number: int | None
    attendance_status: AttendanceStatus
    class_code: str
    class_name: str
    dog: CardDog
    participant_id: uuid.UUID
    participant_name: str
    participant_phone: str | None
    documents: list[DogDocumentResponse]
    rabies_valid_until: date | None
    rabies_valid_for_show: bool | None
    problems: list[str]
    latest_checks: dict[EntryCheckKind, EntryCheckResponse]


class ParticipantCard(BaseModel):
    user_id: uuid.UUID
    display_name: str
    phone: str | None
    email: str | None
    entries: list[EntryCard]


class TicketEntry(BaseModel):
    entry_id: uuid.UUID
    dog_id: uuid.UUID
    dog_name: str
    class_name: str
    catalog_number: int | None
    attendance_status: AttendanceStatus
    problems: list[str]


class TicketResponse(BaseModel):
    show_id: uuid.UUID
    token: str
    entries: list[TicketEntry]


class ScanRequest(BaseModel):
    token: str = Field(..., max_length=200)


class CheckinSummary(BaseModel):
    total: int
    registered: int
    arrived: int
    admitted: int
    rejected: int
    absent: int
