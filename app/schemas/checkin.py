"""Схемы чек-ина и документов собаки."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, EmailStr, model_validator

from app.models.dog import DogDocumentKind
from app.models.show import ShowStaffRole
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
