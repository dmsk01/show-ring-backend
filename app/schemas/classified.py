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

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from app.models.classified import (
    AnimalAvailability,
    ClassifiedCategory,
    ClassifiedPriceKind,
    ClassifiedStatus,
)
from app.models.dog import SexEnum


# ---------------------------------------------------------------------
# Изображения
# ---------------------------------------------------------------------


class ClassifiedImageCreate(BaseModel):
    file_id: uuid.UUID
    # bug_220 audit 2026-05-28: верхняя граница защищает от
    # клиента, присылающего position=2_000_000_000 — это переполнит
    # int4 в БД и сломает ORDER BY position. 100 фотографий в одном
    # объявлении — заведомо больше реалистичного максимума.
    position: int = Field(0, ge=0, le=100)
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


def _validate_price_kind_match(price_kind, price):
    """
    bug_215 audit 2026-05-28: инвариант (price_kind, price). fixed
    требует price > 0; free/negotiable требуют price IS NULL.
    Применяется и в Create, и в Update — общая функция вместо
    дублирования validator'а.
    """
    if price_kind == ClassifiedPriceKind.fixed:
        if price is None or price <= 0:
            raise ValueError(
                "price_kind=fixed requires price > 0"
            )
    else:
        # free / negotiable
        if price is not None:
            raise ValueError(
                f"price_kind={price_kind.value} must have price=null"
            )


class ClassifiedBase(BaseModel):
    category: ClassifiedCategory
    breed_id: uuid.UUID | None = None
    litter_id: uuid.UUID | None = None
    # Пол животного: применим к продаже особи, NULL для услуг/смешанных
    # помётов. Наследуется в Create и Response (оба от ClassifiedBase).
    sex: SexEnum | None = None
    title: str = Field(..., min_length=3, max_length=255)
    description: str = Field(..., min_length=10)
    # bug_215: price имеет смысл только при price_kind=fixed.
    # Default'ный price_kind=fixed сохраняет backwards-compatibility
    # для существующих клиентов, которые присылают только price.
    price: Decimal | None = Field(None, ge=0)
    price_kind: ClassifiedPriceKind = ClassifiedPriceKind.fixed
    city: str | None = Field(None, max_length=128)
    contact_phone: str | None = Field(None, max_length=32)
    contact_email: EmailStr | None = None

    @model_validator(mode="after")
    def _check_price_kind(self) -> "ClassifiedBase":
        _validate_price_kind_match(self.price_kind, self.price)
        return self


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
    # Пол можно проставить/изменить отдельно (например, заполнить у старого
    # объявления). ClassifiedUpdate не наследует Base — поле нужно явно.
    sex: SexEnum | None = None
    title: str | None = Field(None, min_length=3, max_length=255)
    description: str | None = Field(None, min_length=10)
    price: Decimal | None = Field(None, ge=0)
    # bug_215: price_kind можно менять (например, «fixed → negotiable»
    # после неудачных попыток продать по цене). При смене ОБА поля
    # должны прислать вместе — validator ниже это проверяет.
    price_kind: ClassifiedPriceKind | None = None
    city: str | None = Field(None, max_length=128)
    contact_phone: str | None = Field(None, max_length=32)
    contact_email: EmailStr | None = None
    # Смена статуса — это явное действие "закрыть" / "переоткрыть",
    # сервис сам валидирует переходы.
    status: ClassifiedStatus | None = None
    # Доступность животного: свободен / забронирован / продан. В отличие
    # от status, переходы не ограничены — это полностью прерогатива автора
    # (он распоряжается своим животным). Право проверяет _check_owner.
    availability: AnimalAvailability | None = None

    @model_validator(mode="after")
    def _check_price_kind(self) -> "ClassifiedUpdate":
        # bug_215: частичный апдейт price/price_kind легко вводит
        # рассинхрон с CHECK constraint'ом БД. Правило: если в payload
        # есть price_kind, он должен прийти вместе с согласованным
        # price (для fixed — конкретное число, иначе явный null).
        # Если меняется только price без price_kind — клиент должен
        # быть уверен, что текущий kind=fixed; иначе мы тут не знаем,
        # будет ли инвариант нарушен, и доверяем CHECK'у БД отсечь
        # ошибку через IntegrityError.
        if self.price_kind is not None:
            _validate_price_kind_match(self.price_kind, self.price)
        return self


class ClassifiedResponse(ClassifiedBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    author_id: uuid.UUID
    status: ClassifiedStatus
    availability: AnimalAvailability
    views_count: int
    created_at: datetime
    updated_at: datetime
    images: list[ClassifiedImageResponse] = Field(default_factory=list)


class ClassifiedPage(BaseModel):
    items: list[ClassifiedResponse]
    total: int
    page: int
    per_page: int
