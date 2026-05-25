"""
Pydantic-схемы справочников (этап 3).

Делим на 3 группы для каждой сущности:
- Create — то, что приходит на POST. Без id/created_at/updated_at —
  они генерируются БД.
- Update — частичное обновление (PATCH/PUT). Все поля Optional, чтобы
  можно было прислать только то, что меняется.
- Response — то, что отдаём. С id и временами. from_attributes=True
  позволяет валидировать прямо из ORM-объекта (SQLAlchemy → Pydantic).

Зачем разделение Create/Response/Update вместо одной схемы:
- Create запрещает клиенту прислать id (и подделать его).
- Response гарантирует, что в ответе ВСЕГДА есть id и timestamps,
  даже если поле в БД новое и Optional.
- Update делает все поля Optional только в одном месте — не плодим
  ошибок типа "забыл сделать поле Optional".
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------
# AnimalType
# ---------------------------------------------------------------------


class AnimalTypeBase(BaseModel):
    code: str = Field(..., max_length=32, examples=["dog"])
    name: str = Field(..., max_length=128, examples=["Собака"])


class AnimalTypeCreate(AnimalTypeBase):
    pass


class AnimalTypeUpdate(BaseModel):
    code: str | None = Field(None, max_length=32)
    name: str | None = Field(None, max_length=128)


class AnimalTypeResponse(AnimalTypeBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------
# BreedGroup
# ---------------------------------------------------------------------


class BreedGroupBase(BaseModel):
    animal_type_id: uuid.UUID
    number: int = Field(..., ge=1, le=99)
    code: str = Field(..., max_length=64)
    name: str = Field(..., max_length=255)
    description: str | None = None


class BreedGroupCreate(BreedGroupBase):
    pass


class BreedGroupUpdate(BaseModel):
    number: int | None = Field(None, ge=1, le=99)
    code: str | None = Field(None, max_length=64)
    name: str | None = Field(None, max_length=255)
    description: str | None = None


class BreedGroupResponse(BreedGroupBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------
# Breed
# ---------------------------------------------------------------------


class BreedBase(BaseModel):
    animal_type_id: uuid.UUID
    breed_group_id: uuid.UUID | None = None
    code: str = Field(..., max_length=128)
    name: str = Field(..., max_length=255)
    fci_number: str | None = Field(None, max_length=16)
    description: str | None = None


class BreedCreate(BreedBase):
    pass


class BreedUpdate(BaseModel):
    breed_group_id: uuid.UUID | None = None
    code: str | None = Field(None, max_length=128)
    name: str | None = Field(None, max_length=255)
    fci_number: str | None = Field(None, max_length=16)
    description: str | None = None


class BreedResponse(BreedBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------
# ShowClass
# ---------------------------------------------------------------------


class ShowClassBase(BaseModel):
    animal_type_id: uuid.UUID
    code: str = Field(..., max_length=64)
    name: str = Field(..., max_length=128)
    age_from_months: int = Field(..., ge=0, le=360)
    age_to_months: int | None = Field(None, ge=0, le=360)
    can_receive_cac: bool = False
    description: str | None = None


class ShowClassCreate(ShowClassBase):
    pass


class ShowClassUpdate(BaseModel):
    code: str | None = Field(None, max_length=64)
    name: str | None = Field(None, max_length=128)
    age_from_months: int | None = Field(None, ge=0, le=360)
    age_to_months: int | None = Field(None, ge=0, le=360)
    can_receive_cac: bool | None = None
    description: str | None = None


class ShowClassResponse(ShowClassBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------
# ShowRank
# ---------------------------------------------------------------------


class ShowRankBase(BaseModel):
    code: str = Field(..., max_length=64)
    name: str = Field(..., max_length=255)
    description: str | None = None


class ShowRankCreate(ShowRankBase):
    pass


class ShowRankUpdate(BaseModel):
    code: str | None = Field(None, max_length=64)
    name: str | None = Field(None, max_length=255)
    description: str | None = None


class ShowRankResponse(ShowRankBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------
# Title
# ---------------------------------------------------------------------


class TitleBase(BaseModel):
    animal_type_id: uuid.UUID
    code: str = Field(..., max_length=64)
    name: str = Field(..., max_length=128)
    is_reserve: bool = False
    description: str | None = None


class TitleCreate(TitleBase):
    pass


class TitleUpdate(BaseModel):
    code: str | None = Field(None, max_length=64)
    name: str | None = Field(None, max_length=128)
    is_reserve: bool | None = None
    description: str | None = None


class TitleResponse(TitleBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------
# Grade
# ---------------------------------------------------------------------


class GradeBase(BaseModel):
    animal_type_id: uuid.UUID
    code: str = Field(..., max_length=64)
    name: str = Field(..., max_length=128)
    is_disqualifying: bool = False
    is_puppy_grade: bool = False
    description: str | None = None


class GradeCreate(GradeBase):
    pass


class GradeUpdate(BaseModel):
    code: str | None = Field(None, max_length=64)
    name: str | None = Field(None, max_length=128)
    is_disqualifying: bool | None = None
    is_puppy_grade: bool | None = None
    description: str | None = None


class GradeResponse(GradeBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------
# Пагинированный ответ
# ---------------------------------------------------------------------


class PageMeta(BaseModel):
    """
    Метаданные пагинации. Возвращаются вместе со списком, чтобы клиент
    не считал общее количество отдельным запросом.
    """

    total: int
    page: int
    per_page: int


class BreedPage(BaseModel):
    items: list[BreedResponse]
    meta: PageMeta
