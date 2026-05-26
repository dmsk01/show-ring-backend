"""
Модель помёта (этап 5).

Помёт — это группа щенков от одной пары родителей. Хранится отдельно
от собак: щенки могут позже быть оформлены как Dog (после РКФ-актирования),
но сам факт помёта — это публикация заводчика для рынка щенков.

Ключевые решения:
- kennel_id обязательный и CASCADE: помёт существует только в контексте
  питомника. Без питомника он не имеет смысла (никто не отвечает за
  родословную, никто не публикует объявление).
- breed_id обязательный — помёт всегда относится к породе.
  RESTRICT защищает справочник от каскадного удаления.
- father_id / mother_id nullable + SET NULL: родители могут быть из
  другого питомника (внешний кобель), могут быть удалены из БД (например,
  ушли в архив), но сам помёт остаётся видимым.
- price_from / price_to — разрешаем "вилку", т.к. цена за щенков обычно
  зависит от класса (шоу/брид/пэт). Numeric, не Float — деньги не любят
  плавающую точку из-за ошибок округления.
- puppies_count, males_count, females_count — раздельно. По правилам РКФ
  записываются все три числа отдельно, и заводчик их публикует.
- status — отдельный жизненный цикл: planned → born → available → sold_out
  или archived. Доска объявлений потом фильтрует помёты по этому полю.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import (
    Date,
    Enum as SAEnum,
    ForeignKey,
    Integer,
    Numeric,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class LitterStatus(str, enum.Enum):
    planned = "planned"        # запланированная вязка, щенков ещё нет
    born = "born"              # щенки родились, но ещё не выставлены на продажу
    available = "available"    # есть свободные щенки на продажу
    sold_out = "sold_out"      # все щенки распроданы (но запись остаётся для истории)
    archived = "archived"      # архив (старый помёт, скрыт со списков)


class Litter(Base, TimestampMixin):
    __tablename__ = "litters"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    kennel_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        # CASCADE — если питомник удалён, его помёты исчезают вместе с ним.
        # Помёт не существует "сам по себе", его всегда регистрирует
        # конкретный заводчик/питомник.
        ForeignKey("kennels.id", ondelete="CASCADE"),
        index=True,
    )
    breed_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        # RESTRICT, как у dogs.breed_id — справочник пород защищён от
        # случайного каскада. Удалить породу пока есть помёты — ошибка
        # данных, не операция администратора.
        ForeignKey("breeds.id", ondelete="RESTRICT"),
        index=True,
    )

    # Родители — ссылки на dogs. SET NULL: родители могут быть из другого
    # питомника, могут быть удалены из БД позже, но помёт продолжает
    # существовать с пометкой "родитель неизвестен/удалён".
    father_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dogs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    mother_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dogs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Дата рождения щенков. nullable=True для статуса planned: вязка
    # запланирована, но щенки ещё не родились.
    born_at: Mapped[date | None] = mapped_column(Date, nullable=True)

    puppies_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    males_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    females_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Цена за щенка как "от/до". Numeric(10, 2) — до 99 999 999.99,
    # достаточно для любых разумных цен в рублях/долларах/евро.
    price_from: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 2), nullable=True
    )
    price_to: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 2), nullable=True
    )

    status: Mapped[LitterStatus] = mapped_column(
        SAEnum(LitterStatus, name="litterstatus"),
        default=LitterStatus.planned,
        index=True,
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
