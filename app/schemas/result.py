"""
Схемы результатов выставки и титулов (этап 7).

Разделение Create/Update/Response. ShowResult — основная сущность,
DogTitle — сопутствующая (вход — auto, выход — для GET /dogs/{id}/titles).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


class TitleCacheItem(BaseModel):
    """Один элемент titles_cache в результате."""

    code: str
    name: str


class ShowResultBase(BaseModel):
    grade_id: uuid.UUID | None = None
    placement: int | None = Field(None, ge=1, le=99)
    critique: str | None = None


class ShowResultCreate(ShowResultBase):
    show_entry_id: uuid.UUID


class ShowResultUpdate(BaseModel):
    grade_id: uuid.UUID | None = None
    placement: int | None = Field(None, ge=1, le=99)
    critique: str | None = None


class ShowResultResponse(ShowResultBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    show_entry_id: uuid.UUID
    judge_id: uuid.UUID | None
    is_class_winner: bool
    is_best_male: bool
    is_best_female: bool
    is_best_of_breed: bool
    is_best_junior: bool
    is_best_veteran: bool
    is_best_in_group: bool
    is_best_in_show: bool
    titles_cache: list[TitleCacheItem] | None
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------
# Best-of-X (входы для соответствующих эндпоинтов)
# ---------------------------------------------------------------------


class BestOfBreedRequest(BaseModel):
    """
    Выбор ЛПП. Передаём breed_id (порода) и entry_id победителя.
    Сервис верифицирует, что entry относится к этой выставке и породе,
    и что у него есть CW в одном из взрослых классов.
    """

    breed_id: uuid.UUID
    winner_entry_id: uuid.UUID
    # Опционально — best male/female/junior/veteran. Сервис выставит
    # флаги в соответствующих результатах.
    best_male_entry_id: uuid.UUID | None = None
    best_female_entry_id: uuid.UUID | None = None
    best_junior_entry_id: uuid.UUID | None = None
    best_veteran_entry_id: uuid.UUID | None = None


class BestInGroupRequest(BaseModel):
    """Выбор BIG для группы FCI."""

    breed_group_id: uuid.UUID
    winner_entry_id: uuid.UUID


class BestInShowRequest(BaseModel):
    """Выбор BIS — главного победителя выставки."""

    winner_entry_id: uuid.UUID


# ---------------------------------------------------------------------
# DogTitle
# ---------------------------------------------------------------------


class DogTitleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    dog_id: uuid.UUID
    title_id: uuid.UUID
    show_id: uuid.UUID
    judge_id: uuid.UUID | None
    date_earned: date
