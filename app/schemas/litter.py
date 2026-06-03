"""
Схемы помёта (этап 5).

LitterCreate без author_id — владельца определяем через kennel.owner_id
в сервисе, чтобы клиент не мог опубликовать помёт от чужого имени.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.litter import LitterStatus
from app.schemas.dog import DogRef


class LitterBase(BaseModel):
    kennel_id: uuid.UUID
    breed_id: uuid.UUID
    father_id: uuid.UUID | None = None
    mother_id: uuid.UUID | None = None
    born_at: date | None = None
    # ge=0 — могут быть "вязка запланирована", но детальные числа уже
    # известны (заводчик планирует). le=30 — практический потолок для
    # одного помёта, защита от опечатки (200 щенков).
    puppies_count: int | None = Field(None, ge=0, le=30)
    males_count: int | None = Field(None, ge=0, le=30)
    females_count: int | None = Field(None, ge=0, le=30)
    price_from: Decimal | None = Field(None, ge=0)
    price_to: Decimal | None = Field(None, ge=0)
    status: LitterStatus = LitterStatus.planned
    description: str | None = None

    @model_validator(mode="after")
    def _validate_price_range(self) -> "LitterBase":
        # price_to не может быть меньше price_from. Лучше отрубить
        # на входе, чем потом разбираться с "странной" фильтрацией.
        if (
            self.price_from is not None
            and self.price_to is not None
            and self.price_to < self.price_from
        ):
            raise ValueError("price_to must be >= price_from")
        return self


class LitterCreate(LitterBase):
    pass


class LitterUpdate(BaseModel):
    # Все поля Optional — частичное обновление через PUT. kennel_id и
    # breed_id менять не разрешаем после создания, иначе теряется смысл
    # помёта как записи "от такого-то питомника, такой-то породы".
    father_id: uuid.UUID | None = None
    mother_id: uuid.UUID | None = None
    born_at: date | None = None
    puppies_count: int | None = Field(None, ge=0, le=30)
    males_count: int | None = Field(None, ge=0, le=30)
    females_count: int | None = Field(None, ge=0, le=30)
    price_from: Decimal | None = Field(None, ge=0)
    price_to: Decimal | None = Field(None, ge=0)
    status: LitterStatus | None = None
    description: str | None = None

    @model_validator(mode="after")
    def _validate_price_range(self) -> "LitterUpdate":
        # bug_218 audit 2026-05-28: тот же инвариант, что и в LitterBase.
        # PUT-обновление мог поставить price_from=1000, price_to=500.
        # NB: тут проверяем ТОЛЬКО если оба поля присутствуют в payload.
        # Частичные апдейты только одного из price_* не валидируются;
        # семантически они могут оставить базу в инвалидном состоянии,
        # но это уже не дело Pydantic'а — нужна проверка после merge'а
        # на стороне сервиса. Минимум здесь — отсекать явно битые.
        if (
            self.price_from is not None
            and self.price_to is not None
            and self.price_to < self.price_from
        ):
            raise ValueError("price_to must be >= price_from")
        return self


class LitterResponse(LitterBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime
    updated_at: datetime
    # Этап 18: развёрнутые родители (id/name/avatar) — чтобы фронт не делал
    # доп. запрос /dogs/{id} ради имени. father_id/mother_id остаются.
    father: DogRef | None = None
    mother: DogRef | None = None


class LitterPage(BaseModel):
    items: list[LitterResponse]
    total: int
    page: int
    per_page: int
