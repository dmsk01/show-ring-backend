"""
Схемы питомника (этап 4).

KennelCreate без owner_id: владельца ставим из current_user в роутере,
а не доверяем клиенту. Иначе любой авторизованный пользователь смог
бы создать питомник от имени другого юзера.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class KennelBase(BaseModel):
    name: str = Field(..., max_length=255)
    kennel_prefix: str | None = Field(None, max_length=128)
    description: str | None = None
    city: str | None = Field(None, max_length=128)
    country: str | None = Field(None, max_length=64)
    contact_phone: str | None = Field(None, max_length=32)
    contact_email: EmailStr | None = None
    # Используем str + regex-проверку, а не HttpUrl: HttpUrl при
    # model_dump(mode="json") возвращает объект Url, а в режиме python
    # — конкретный тип, который asyncpg не умеет биндить в VARCHAR.
    # Простая строка с regex-валидацией покрывает наши требования.
    website: str | None = Field(None, max_length=255, pattern=r"^https?://.+")


class KennelCreate(KennelBase):
    # owner_id НЕ принимаем от клиента — см. модуль-докстринг.
    pass


class KennelUpdate(BaseModel):
    name: str | None = Field(None, max_length=255)
    kennel_prefix: str | None = Field(None, max_length=128)
    description: str | None = None
    city: str | None = Field(None, max_length=128)
    country: str | None = Field(None, max_length=64)
    contact_phone: str | None = Field(None, max_length=32)
    contact_email: EmailStr | None = None
    # Используем str + regex-проверку, а не HttpUrl: HttpUrl при
    # model_dump(mode="json") возвращает объект Url, а в режиме python
    # — конкретный тип, который asyncpg не умеет биндить в VARCHAR.
    # Простая строка с regex-валидацией покрывает наши требования.
    website: str | None = Field(None, max_length=255, pattern=r"^https?://.+")
    avatar_file_id: uuid.UUID | None = None


class KennelResponse(KennelBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    owner_id: uuid.UUID
    avatar_file_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    # Этап 18: метка «проверенный питомник» (модерация, этап 12) + агрегаты
    # для карточек витрины. is_verified читается из ORM; счётчики
    # проставляются роутером (пачкой на списках, без N+1).
    is_verified: bool = False
    dogs_count: int = 0
    litters_count: int = 0
