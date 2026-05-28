"""
Модели результатов выставки и титулов собаки (этап 7).

Архитектура. Два уровня:

1. `ShowResult` — результат конкретной записи (ShowEntry) в ринге:
   оценка эксперта + место + описание + флаги "лучший X". Один результат
   на одну запись (UNIQUE). `titles_cache` — JSONB-кэш для быстрого
   отображения списка титулов в карточке результата (чтобы не делать
   JOIN на dog_titles при рендере каталога).

2. `DogTitle` — единственный источник истины о титулах собаки. При
   присвоении титула делается атомарная запись:
   INSERT show_results → INSERT dog_titles → UPDATE titles_cache, всё
   в одной транзакции. Если что-то пошло не так — откатывается всё.

Логика "best_*" хранится флагами в ShowResult. Так "ЛПП", "BIG", "BIS"
конкретной собаки виден на её записи в её классе:

- is_class_winner       — CW, победитель класса
- is_best_male/female   — ЛК / ЛС в породе
- is_best_of_breed      — BOB (ЛПП), лучший представитель породы
- is_best_junior        — лучший юниор породы (отдельный титул)
- is_best_veteran       — лучший ветеран породы
- is_best_in_group      — BIG, победитель группы FCI
- is_best_in_show       — BIS, победитель выставки

Альтернатива (отдельная таблица ShowBest) дала бы аккуратнее модель,
но потребовала бы JOIN на каждом запросе результата. Флаги дешевле
по чтению, а INSERT всё равно делаются по одному ringу за раз.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import (
    Boolean,
    Date,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class ShowResult(Base, TimestampMixin):
    """
    Результат записи в ринге.

    Решения:
    - show_entry_id UNIQUE: один результат на запись. CASCADE — отмена
      записи (или удаление выставки) уносит результат.
    - placement nullable: судья может ввести только оценку, расстановку
      сделать позже. После CW определяется по placement=1 + grade=excellent.
    - grade_id nullable до ввода: при создании "пустого" результата перед
      началом ринга. На практике грейд ставится сразу — оставляем nullable
      для гибкости.
    - judge_id nullable: иногда фиксируем результаты задним числом, когда
      судья уже не в системе. Лучше потерять связь, чем терять результат.
    - titles_cache как JSONB: список объектов вида
      [{"code": "CAC", "name": "CAC"}, {"code": "CW", "name": "CW"}].
      Это денормализация ради скорости рендера каталога результатов.
    """

    __tablename__ = "show_results"
    __table_args__ = (
        UniqueConstraint("show_entry_id", name="uq_show_result_entry"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    show_entry_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("show_entries.id", ondelete="CASCADE"),
        index=True,
    )
    judge_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    grade_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("grades.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    # Место в ринге класса: 1..4 для основных, дальше "не призовое".
    # Nullable, пока расстановка не сделана.
    placement: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Флаги "лучших". См. модульный докстринг.
    is_class_winner: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    is_best_male: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    is_best_female: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    is_best_of_breed: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    is_best_junior: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    is_best_veteran: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    is_best_in_group: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    is_best_in_show: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )

    critique: Mapped[str | None] = mapped_column(Text, nullable=True)

    # JSONB — кэш списка титулов, привязанных к этому результату.
    # Источник истины — таблица dog_titles, кэш обновляется в одной
    # транзакции при INSERT'е титулов.
    # Хранится в формате: [{"code": "...", "name": "..."}, ...]
    titles_cache: Mapped[list[dict] | None] = mapped_column(
        JSONB, nullable=True
    )


class DogTitle(Base):
    """
    Запись о присвоенном собаке титуле.

    Это **источник истины** для всех "сертификатов" собаки. Карточка
    собаки и доступные классы для записи (см. этап 6, working/champions)
    опираются на эту таблицу.

    Решения:
    - UNIQUE (dog_id, title_id, show_id): один и тот же титул на одной
      выставке не присваивается дважды (например, CAC на одной выставке —
      ровно один). Между выставками — отдельные записи.
    - show_id обязателен: каждый титул "родился" на конкретной выставке.
      Если в будущем появятся титулы, выдаваемые без выставки (например,
      по статистике побед — "Чемпион РКФ"), сделаем show_id nullable.
    - date_earned дублирует show.date_start, но удобнее держать рядом:
      запросы "титулы за год" — без JOIN на shows.
    """

    __tablename__ = "dog_titles"
    __table_args__ = (
        UniqueConstraint(
            "dog_id", "title_id", "show_id", name="uq_dog_title_show"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    dog_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        # CASCADE — если собака удалена, её титулы тоже исчезают.
        # Историческая ценность невелика без самой собаки.
        ForeignKey("dogs.id", ondelete="CASCADE"),
        index=True,
    )
    title_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("titles.id", ondelete="RESTRICT"),
        index=True,
    )
    show_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        # RESTRICT — нельзя удалить выставку, на которой выданы титулы.
        # Защита от потери исторических данных.
        ForeignKey("shows.id", ondelete="RESTRICT"),
        index=True,
    )
    judge_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        # bug_223 audit 2026-05-28: индекс под отчёты «титулы выданные
        # судьёй X» и каскад SET NULL при удалении user'а.
        index=True,
    )
    date_earned: Mapped[date] = mapped_column(Date, index=True)
