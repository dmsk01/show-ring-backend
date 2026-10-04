"""Схемы журнала согласий (app/services/consent.py)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Через /users/me/consents выдаются только согласия уровня аккаунта.
# Согласие на распространение — переключателем contacts_public у
# конкретной публикации (ч. 1 ст. 10.1: отдельно от иных согласий).
AccountConsentKind = Literal["terms", "personal_data"]


class ConsentItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    kind: str
    revision: str
    target_id: uuid.UUID | None
    granted_at: datetime


class ConsentsResponse(BaseModel):
    active: list[ConsentItem]
    # Обязательные согласия, которых нет в актуальной редакции: фронт
    # показывает по ним диалог подтверждения.
    missing: list[str]


class ConsentGrantRequest(BaseModel):
    kinds: list[AccountConsentKind] = Field(min_length=1)
