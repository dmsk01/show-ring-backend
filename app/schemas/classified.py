"""
Схемы доски объявлений (этап 5).

Разделение Create / Update / Response:
- author_id и views_count никогда не приходят от клиента — выставляются
  в сервисе (current_user.id) и в БД (server_default).
- В Response отдаём views_count и список картинок (через
  ClassifiedImageResponse).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models.classified import ClassifiedCategory, ClassifiedStatus


# ---------------------------------------------------------------------
# Изображения
# ---------------------------------------------------------------------


class ClassifiedImageCreate(BaseModel):
    file_id: uuid.UUID
    position: int = Field(0, ge=0)
    is_primary: bool = False


class ClassifiedImageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    file_id: uuid.UUID
    position: int
    is_primary: bool


# ---------------------------------------------------------------------
# Classified
# ---------------------------------------------------------------------


class ClassifiedBase(BaseModel):
    category: ClassifiedCategory
    breed_id: uuid.UUID | None = None
    litter_id: uuid.UUID | None = None
    title: str = Field(..., min_length=3, max_length=255)
    description: str = Field(..., min_length=10)
    price: Decimal | None = Field(None, ge=0)
    city: str | None = Field(None, max_length=128)
    contact_phone: str | None = Field(None, max_length=32)
    contact_email: EmailStr | None = None


class ClassifiedCreate(ClassifiedBase):
    # Картинки можно передать списком сразу в POST: клиент сначала
    # загрузил файлы через /files/upload, получил id, и теперь
    # привязывает их к объявлению.
    images: list[ClassifiedImageCreate] = Field(default_factory=list)


class ClassifiedUpdate(BaseModel):
    # Категорию менять не запрещаем явно, но обычно не нужно — это
    # фактически "другое объявление". Оставляем гибко.
    category: ClassifiedCategory | None = None
    breed_id: uuid.UUID | None = None
    litter_id: uuid.UUID | None = None
    title: str | None = Field(None, min_length=3, max_length=255)
    description: str | None = Field(None, min_length=10)
    price: Decimal | None = Field(None, ge=0)
    city: str | None = Field(None, max_length=128)
    contact_phone: str | None = Field(None, max_length=32)
    contact_email: EmailStr | None = None
    # Смена статуса — это явное действие "закрыть" / "переоткрыть",
    # сервис сам валидирует переходы.
    status: ClassifiedStatus | None = None


class ClassifiedResponse(ClassifiedBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    author_id: uuid.UUID
    status: ClassifiedStatus
    views_count: int
    created_at: datetime
    updated_at: datetime
    images: list[ClassifiedImageResponse] = Field(default_factory=list)


class ClassifiedPage(BaseModel):
    items: list[ClassifiedResponse]
    total: int
    page: int
    per_page: int
