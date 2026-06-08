"""
Модели доски объявлений (этап 5).

Универсальная доска: продажа щенков/взрослых собак, услуги хендлера,
вязка, груминг, прочее. Объявления привязаны к категории и опционально
к породе/городу/помёту. Поиск — полнотекстовый PostgreSQL (tsvector).

Архитектурные решения:

- search_vector как GENERATED COLUMN: PostgreSQL сам пересчитывает
  tsvector при INSERT/UPDATE из title+description. Альтернатива —
  триггер на BEFORE INSERT/UPDATE — менее декларативна. Альтернатива
  "GIN-индекс на выражение" — индекс работает, но в запросе всегда
  пришлось бы повторять выражение to_tsvector(...) дословно, иначе
  планер не использовал бы индекс. Persisted-вариант проще и быстрее
  на чтении.

- 'russian' конфигурация полнотекстового поиска — встроенная в PG
  (snowball stemmer), снимает окончания: "щенок", "щенки", "щенков"
  → один корень "щенк".

- views_count как Integer: счётчик просмотров инкрементируется атомарным
  UPDATE … SET views_count = views_count + 1 — это race-condition-safe
  на уровне БД.

- Статусная модель: active → moderation → closed → archived. Модерация
  не реализуется в 5 этапе, но поле уже есть, чтобы потом не делать
  миграцию.

- classified_images отдельной таблицей — по аналогии с dog_photos: даёт
  порядок (position) и флаг главного фото (is_primary). UNIQUE на пару
  (classified_id, file_id) защищает от случайной дублей.
"""

from __future__ import annotations

import enum
import uuid
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.dog import SexEnum


class ClassifiedCategory(str, enum.Enum):
    puppy_sale = "puppy_sale"      # продажа щенков
    adult_sale = "adult_sale"      # продажа взрослой собаки
    mating = "mating"              # предложение/поиск вязки
    handler = "handler"            # услуги хендлера
    grooming = "grooming"          # услуги груминга
    other = "other"                # прочее (резерв)


class ClassifiedStatus(str, enum.Enum):
    active = "active"              # активно и показывается публично
    moderation = "moderation"      # ждёт проверки модератором (на будущее)
    closed = "closed"              # закрыто автором (продано, отозвано)
    archived = "archived"          # архив (старше N месяцев, скрыто)


class ClassifiedPriceKind(str, enum.Enum):
    """
    bug_215 audit 2026-05-28: устраняет двусмысленность «price=NULL vs
    price=0 vs price>0». Раньше клиент сам угадывал, что имел в виду
    автор. Теперь три явных смысла:

    - fixed       — конкретная цена; в БД price IS NOT NULL и > 0
    - free        — бесплатно / в добрые руки; price IS NULL
    - negotiable  — цена договорная / по запросу; price IS NULL

    Инвариант защищён CHECK-constraint'ом на уровне БД (миграция
    6b1f4e8a3d92), Pydantic-валидатор отсекает невалидные комбинации
    ещё до запроса.
    """

    fixed = "fixed"
    free = "free"
    negotiable = "negotiable"


class Classified(Base, TimestampMixin):
    __tablename__ = "classifieds"
    __table_args__ = (
        # GIN — единственный тип индекса, который умеет искать по tsvector
        # через @@. Без него запросы full-text всегда будут seq scan.
        Index(
            "ix_classifieds_search_vector",
            "search_vector",
            postgresql_using="gin",
        ),
        # Композитный индекс под типичный фильтр "активные объявления
        # в категории, отсортированные по дате". Помогает планнеру выбрать
        # одно сканирование вместо двух.
        Index("ix_classifieds_status_category", "status", "category"),
        # bug_215 audit: price согласован с price_kind. CHECK на
        # уровне БД защищает от прямых SQL-апдейтов в обход сервиса.
        CheckConstraint(
            "(price_kind = 'fixed' AND price IS NOT NULL AND price > 0) "
            "OR (price_kind IN ('free', 'negotiable') AND price IS NULL)",
            name="ck_classifieds_price_kind_match",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    author_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        # CASCADE — если автор удалил аккаунт, его объявления уходят
        # вместе с ним. Это не "историческая запись" (как у собак),
        # а пользовательский контент.
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    category: Mapped[ClassifiedCategory] = mapped_column(
        SAEnum(ClassifiedCategory, name="classifiedcategory"),
        index=True,
    )
    # Порода опциональна: услуги хендлера/груминга могут быть породо-
    # независимыми. SET NULL — если породу удалили из справочника,
    # объявление не пропадает, просто "без породы".
    breed_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("breeds.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # litter_id — если объявление о продаже щенков от конкретного помёта,
    # связь даёт доступ к родословной, фото родителей, дате рождения.
    # SET NULL: помёт может быть удалён, объявление остаётся.
    litter_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("litters.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Пол животного в объявлении. nullable: применим только к продаже
    # конкретной особи (adult_sale/mating) и к щенку определённого пола.
    # Для услуг (handler/grooming) и помётов «вперемешку» (и кобели, и
    # суки в одном объявлении) остаётся NULL — точечный фильтр ?sex= их
    # не показывает. Переиспользуем SexEnum собак (PG-тип 'sexenum'),
    # а не заводим второй тип под тот же концепт «пол животного».
    sex: Mapped[SexEnum | None] = mapped_column(
        SAEnum(SexEnum, name="sexenum"), nullable=True, index=True
    )

    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text)

    price: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 2), nullable=True
    )
    # bug_215: price_kind различает три семантики; см. ClassifiedPriceKind
    # докстринг и CHECK-constraint в __table_args__.
    price_kind: Mapped[ClassifiedPriceKind] = mapped_column(
        SAEnum(ClassifiedPriceKind, name="classifiedpricekind"),
        default=ClassifiedPriceKind.fixed,
        server_default=ClassifiedPriceKind.fixed.value,
    )
    city: Mapped[str | None] = mapped_column(
        String(128), index=True, nullable=True
    )

    status: Mapped[ClassifiedStatus] = mapped_column(
        SAEnum(ClassifiedStatus, name="classifiedstatus"),
        default=ClassifiedStatus.active,
        index=True,
    )
    # server_default="0" чтобы существующие строки при alter получили 0,
    # а не NULL. На новый INSERT работает и default=0, и server_default.
    views_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )

    contact_phone: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    contact_email: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )

    # Генерируется PostgreSQL автоматически из title и description.
    # coalesce защищает от NULL — description обязателен, но на всякий
    # случай (если кто-то изменит схему). 'russian' — встроенная FTS-
    # конфигурация PG (snowball-stemmer для русского).
    search_vector: Mapped[str | None] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('russian', "
            "coalesce(title, '') || ' ' || coalesce(description, ''))",
            persisted=True,
        ),
        nullable=True,
    )

    images: Mapped[list["ClassifiedImage"]] = relationship(
        back_populates="classified",
        cascade="all, delete-orphan",
        order_by="ClassifiedImage.position",
    )


class ClassifiedImage(Base):
    """
    Фото в объявлении. Аналог DogPhoto — отдельная таблица с position
    и is_primary, чтобы рендерить галерею в нужном порядке и знать,
    какое фото показывать на превью.
    """

    __tablename__ = "classified_images"
    __table_args__ = (
        UniqueConstraint(
            "classified_id", "file_id", name="uq_classified_image"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    classified_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("classifieds.id", ondelete="CASCADE"),
        index=True,
    )
    file_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("files.id", ondelete="CASCADE"),
        index=True,
    )
    position: Mapped[int] = mapped_column(Integer, default=0)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)

    classified: Mapped["Classified"] = relationship(back_populates="images")
