"""Схемы админского CRUD лимитов квот загрузки."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class UploadQuotaTierResponse(BaseModel):
    tier: str
    daily_limit: int
    max_storage_bytes: int

    model_config = ConfigDict(from_attributes=True)


class UploadQuotaUpdate(BaseModel):
    """Новые лимиты тира. Оба поля обязательны и положительны."""

    daily_limit: int = Field(gt=0)
    max_storage_bytes: int = Field(gt=0)
