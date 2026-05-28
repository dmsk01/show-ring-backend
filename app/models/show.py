"""
Модели выставки (этап 6).

Архитектура. Выставка — это event с несколькими "плоскостями":
1. Сама выставка (Show): организатор, ранг, дата, место, статус.
2. Допущенные породы (ShowBreed) — если таблица пуста, выставка
   всепородная. Если есть строки — список явных allow-list для
   монопородных и групповых.
3. Судьи (ShowJudge) — many-to-many через association table. Каждый
   судья назначен на конкретную породу ИЛИ группу пород (одно из двух).
4. Расписание рингов (ShowRing) — конкретное расписание для пары
   "порода + класс" с назначенным судьёй.
5. Записи участников (ShowEntry) — собака + класс, выбранный владельцем.
   catalog_number — порядковый номер в каталоге, присваивается при
   закрытии регистрации (или на лету).

Решения:

- Статусная модель Show вынесена в Enum (ShowStatus). Переходы
  валидируются в сервисе, БД хранит только текущее значение.

- show_judges: судья назначается ИЛИ на конкретную породу (breed_id),
  ИЛИ на группу пород (breed_group_id), но не на оба сразу.
  В БД это валидирует CHECK-constraint, в сервисе — отдельная проверка.

- show_entries.show_class_id обязательный (NOT NULL). Класс выбирает
  владелец из доступных по возрасту — система не должна решать за него.
  По правилам РКФ собака в 15-18 мес может пойти в юниоры или
  открытый — это выбор хендлера/владельца.

- UNIQUE(show_id, dog_id) на show_entries — собака не может быть
  записана дважды на одну выставку. ON DELETE CASCADE на show:
  отмена выставки уносит все записи.

- catalog_number nullable: до закрытия регистрации номеров нет.
  При status=registration_closed сервис прогоняет нумерацию.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, time
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    Enum as SAEnum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class ShowStatus(str, enum.Enum):
    draft = "draft"                              # черновик, организатор настраивает
    registration_open = "registration_open"      # открыт приём записей
    registration_closed = "registration_closed"  # запись закрыта, номера каталога присвоены
    in_progress = "in_progress"                  # выставка идёт (этап 7 — оценки)
    completed = "completed"                      # завершена, результаты опубликованы
    cancelled = "cancelled"                      # отменена


class Show(Base, TimestampMixin):
    __tablename__ = "shows"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    organizer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        # RESTRICT — нельзя удалить юзера-организатора, пока есть его
        # выставки. Историчность важнее ергономики удаления аккаунта.
        ForeignKey("users.id", ondelete="RESTRICT"),
        index=True,
    )
    rank_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        # RESTRICT — справочник рангов защищён от каскада.
        ForeignKey("show_ranks.id", ondelete="RESTRICT"),
        index=True,
    )

    name: Mapped[str] = mapped_column(String(255), index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    date_start: Mapped[date] = mapped_column(Date, index=True)
    # date_end nullable: однодневные выставки.
    date_end: Mapped[date | None] = mapped_column(Date, nullable=True)

    city: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    country: Mapped[str | None] = mapped_column(String(64), nullable=True)
    venue: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Стоимость участия. Может варьироваться по классам — пока одна цена
    # за запись (этап 6). Усложнение через ShowFeeTier — на будущее.
    entry_fee: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 2), nullable=True
    )

    # Дедлайн онлайн-регистрации. После него ShowEntry создавать нельзя
    # (валидация в сервисе). Отдельно от date_start, потому что обычно
    # регистрация закрывается за неделю-две до выставки.
    registration_deadline: Mapped[date | None] = mapped_column(
        Date, nullable=True
    )

    status: Mapped[ShowStatus] = mapped_column(
        SAEnum(ShowStatus, name="showstatus"),
        default=ShowStatus.draft,
        index=True,
    )

    # Allow-list пород. Если пуст — выставка всепородная.
    breeds: Mapped[list["ShowBreed"]] = relationship(
        back_populates="show",
        cascade="all, delete-orphan",
    )
    judges: Mapped[list["ShowJudge"]] = relationship(
        back_populates="show",
        cascade="all, delete-orphan",
    )
    rings: Mapped[list["ShowRing"]] = relationship(
        back_populates="show",
        cascade="all, delete-orphan",
    )
    entries: Mapped[list["ShowEntry"]] = relationship(
        back_populates="show",
        cascade="all, delete-orphan",
    )


class ShowBreed(Base):
    """
    Allow-list пород для выставки. Если для выставки нет ни одной строки —
    она считается всепородной (любая порода допускается).

    Зачем не использовать ARRAY-колонку в shows: отдельная таблица даёт
    индекс на breed_id (быстрая проверка "допущена ли"), и в будущем
    легко расширить полями (например, лимит записей по породе).
    """

    __tablename__ = "show_breeds"
    __table_args__ = (
        UniqueConstraint("show_id", "breed_id", name="uq_show_breed"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    show_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("shows.id", ondelete="CASCADE"),
        index=True,
    )
    breed_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        # RESTRICT — справочник защищён.
        ForeignKey("breeds.id", ondelete="RESTRICT"),
        index=True,
    )

    show: Mapped["Show"] = relationship(back_populates="breeds")


class ShowJudge(Base):
    """
    Назначение судьи на выставку.

    Судья назначается ровно на одну сущность: либо на конкретную породу
    (breed_id), либо на группу пород (breed_group_id). CHECK-constraint
    на уровне БД защищает от случая "оба заданы" / "ни один не задан".

    Зачем не разделять на ShowJudgeBreed и ShowJudgeGroup — это две
    таблицы с почти одинаковой структурой, что мешает писать запросы
    "все назначения судьи на этой выставке". Унификация через
    constraint удобнее.
    """

    __tablename__ = "show_judges"
    __table_args__ = (
        # XOR на breed_id и breed_group_id: ровно одно из них задано.
        CheckConstraint(
            "(breed_id IS NOT NULL AND breed_group_id IS NULL) "
            "OR (breed_id IS NULL AND breed_group_id IS NOT NULL)",
            name="ck_show_judge_target",
        ),
        # Один судья не может быть назначен на одну и ту же породу
        # дважды на одной выставке. На NULL UNIQUE не срабатывает,
        # поэтому отдельные UNIQUE на пары.
        UniqueConstraint(
            "show_id", "judge_id", "breed_id", name="uq_show_judge_breed"
        ),
        UniqueConstraint(
            "show_id",
            "judge_id",
            "breed_group_id",
            name="uq_show_judge_group",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    show_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("shows.id", ondelete="CASCADE"),
        index=True,
    )
    judge_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        # RESTRICT — нельзя удалить юзера-судью, пока есть его назначения.
        ForeignKey("users.id", ondelete="RESTRICT"),
        index=True,
    )
    breed_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("breeds.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    breed_group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("breed_groups.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )

    show: Mapped["Show"] = relationship(back_populates="judges")


class ShowRing(Base):
    """
    Расписание ринга: где и когда судья будет оценивать какую породу
    и какой класс.

    Решения:
    - judge_id опционален: организатор может сначала забить расписание
      по породам и классам, а назначить судей позже.
    - time-only поля (без даты): дата ринга — это date_start/date_end
      выставки. Если выставка многодневная, имеет смысл хранить
      ring_date — оставляю как date | None для гибкости.
    """

    __tablename__ = "show_rings"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    show_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("shows.id", ondelete="CASCADE"),
        index=True,
    )
    # Номер ринга на выставке (физически — ринг №3 на территории).
    ring_number: Mapped[int] = mapped_column(Integer)
    breed_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("breeds.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    breed_group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("breed_groups.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    show_class_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("show_classes.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    judge_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
        # bug_221 audit 2026-05-28: FK без индекса = full scan на
        # любой WHERE judge_id = ? и каскадных удалениях user'а.
        index=True,
    )

    ring_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    time_start: Mapped[time | None] = mapped_column(Time, nullable=True)
    time_end: Mapped[time | None] = mapped_column(Time, nullable=True)
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)

    show: Mapped["Show"] = relationship(back_populates="rings")


class ShowEntry(Base, TimestampMixin):
    """
    Запись собаки на выставку.

    Ключевое: show_class_id выбирает владелец из доступных по возрасту.
    Сервис валидирует, что выбранный класс входит в список разрешённых.

    catalog_number — присваивается при закрытии регистрации (или на лету
    при записи, зависит от политики организатора). nullable до этого момента.
    """

    __tablename__ = "show_entries"
    __table_args__ = (
        # Одну собаку нельзя записать дважды на одну выставку.
        UniqueConstraint("show_id", "dog_id", name="uq_show_entry_dog"),
        # catalog_number должен быть уникален в рамках выставки.
        UniqueConstraint(
            "show_id", "catalog_number", name="uq_show_entry_catalog"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    show_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("shows.id", ondelete="CASCADE"),
        index=True,
    )
    dog_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        # CASCADE — если собаку удалили из системы, запись на выставку
        # тоже исчезает. Историю результатов будем хранить в отдельной
        # таблице result в этапе 7.
        ForeignKey("dogs.id", ondelete="CASCADE"),
        index=True,
    )
    show_class_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("show_classes.id", ondelete="RESTRICT"),
        index=True,
    )
    # handler — кто ведёт собаку в ринге. Опционально: обычно владелец сам.
    handler_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        # bug_222 audit 2026-05-28: индекс под выборки «записи, где
        # хендлер = X» и каскад SET NULL.
        index=True,
    )
    # Кто записал собаку (для аудита и прав на отмену). Обычно владелец
    # её питомника, но возможны случаи "записал хендлер".
    registered_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        index=True,
    )
    catalog_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    show: Mapped["Show"] = relationship(back_populates="entries")
