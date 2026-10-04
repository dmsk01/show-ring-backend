"""Схемы чек-ина и документов собаки."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict

from app.models.dog import DogDocumentKind


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
