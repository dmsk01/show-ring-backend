"""
Схемы собак и родословной (этап 4).

PedigreeNode рекурсивная — для дерева 3-4 поколений. Pydantic v2
поддерживает forward refs через model_rebuild().
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.dog import SexEnum


class DogBase(BaseModel):
    kennel_id: uuid.UUID | None = None
    breed_id: uuid.UUID
    name: str = Field(..., max_length=255)
    sex: SexEnum
    date_of_birth: date | None = None
    color: str | None = Field(None, max_length=128)
    rkf_number: str | None = Field(None, max_length=64)
    tattoo: str | None = Field(None, max_length=64)
    microchip: str | None = Field(None, max_length=32)
    father_id: uuid.UUID | None = None
    mother_id: uuid.UUID | None = None
    description: str | None = None


class DogCreate(DogBase):
    pass


class DogUpdate(BaseModel):
    kennel_id: uuid.UUID | None = None
    breed_id: uuid.UUID | None = None
    name: str | None = Field(None, max_length=255)
    sex: SexEnum | None = None
    date_of_birth: date | None = None
    color: str | None = Field(None, max_length=128)
    rkf_number: str | None = Field(None, max_length=64)
    tattoo: str | None = Field(None, max_length=64)
    microchip: str | None = Field(None, max_length=32)
    father_id: uuid.UUID | None = None
    mother_id: uuid.UUID | None = None
    description: str | None = None


class DogResponse(DogBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime
    updated_at: datetime


class DogShort(BaseModel):
    """
    Краткая карточка собаки — для списков, родословной.
    Не несём description/photos, чтобы не раздувать JSON.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    sex: SexEnum
    date_of_birth: date | None
    breed_id: uuid.UUID
    rkf_number: str | None


class PedigreeNode(BaseModel):
    """
    Узел дерева родословной. None в father/mother значит "родитель
    неизвестен" — это валидный кейс (привозная собака).
    """

    id: uuid.UUID
    name: str
    sex: SexEnum
    date_of_birth: date | None = None
    breed_id: uuid.UUID
    rkf_number: str | None = None
    father: "PedigreeNode | None" = None
    mother: "PedigreeNode | None" = None


PedigreeNode.model_rebuild()


class DogPage(BaseModel):
    items: list[DogResponse]
    total: int
    page: int
    per_page: int
