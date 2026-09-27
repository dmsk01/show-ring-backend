"""
Схемы собак и родословной (этап 4).

PedigreeNode рекурсивная — для дерева 3-4 поколений. Pydantic v2
поддерживает forward refs через model_rebuild().
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Literal

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
    litter_id: uuid.UUID | None = None
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
    litter_id: uuid.UUID | None = None
    description: str | None = None


class DogImageCreate(BaseModel):
    """Привязка уже загруженного файла к собаке (см. POST /dogs/{id}/images)."""

    file_id: uuid.UUID
    position: int = 0
    is_primary: bool = False


class DogResponse(DogBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    # Владелец карточки (тот, кто добавил собаку). None — у легаси-собак,
    # для которых владельца не удалось сопоставить. Фронт прячет кнопки
    # «изменить»/«удалить» у чужих собак.
    owner_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime
    # Фото (этап 18). avatar_file_id — главное фото (is_primary, иначе первое),
    # photo_file_ids — вся галерея по position. Оба выводятся из dog_photos,
    # без отдельной колонки. Файлы публичны: GET /files/{id}.
    avatar_file_id: uuid.UUID | None = None
    photo_file_ids: list[uuid.UUID] = Field(default_factory=list)

    @classmethod
    def from_orm_with_photos(cls, dog, photos) -> "DogResponse":
        """DogResponse из ORM-собаки + список её DogPhoto (avatar/галерея)."""
        ordered = sorted(photos, key=lambda p: p.position)
        ids = [p.file_id for p in ordered]
        avatar = next(
            (p.file_id for p in ordered if p.is_primary), None
        ) or (ids[0] if ids else None)
        resp = cls.model_validate(dog)
        resp.avatar_file_id = avatar
        resp.photo_file_ids = ids
        return resp


class DogRef(BaseModel):
    """Краткая ссылка на собаку (родитель помёта). avatar_file_id — главное фото."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    avatar_file_id: uuid.UUID | None = None


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


class DogRelative(BaseModel):
    """
    Родственник в списках «Потомки»/«Сибсы». Родство не хранится отдельно —
    выводится из father_id/mother_id, поэтому всегда согласовано с родословной.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    sex: SexEnum
    date_of_birth: date | None = None
    breed_id: uuid.UUID
    rkf_number: str | None = None
    owner_id: uuid.UUID | None = None
    avatar_file_id: uuid.UUID | None = None


class DogDescendant(DogRelative):
    # Второй родитель потомка (для «от кого»): мать, если собака — отец, и
    # наоборот. None — второй родитель неизвестен.
    other_parent: DogRef | None = None


class DogSibling(DogRelative):
    # full — оба родителя общие; half — только один (shared_parent говорит какой).
    kind: Literal["full", "half"]
    shared_parent: Literal["father", "mother", "both"]


class DescendantLink(BaseModel):
    """POST /dogs/{id}/descendants — сделать существующую собаку потомком."""

    child_id: uuid.UUID


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
