"""
Модель собаки и связь с фото (этап 4).

Ключевая особенность — self-referential FK для родословной:
father_id и mother_id ссылаются на dogs.id. Это базовый паттерн
"дерево" в реляционной модели, на котором мы потом построим
рекурсивный CTE для запроса нескольких поколений.

Решения:
- sex как Enum — строго male/female, защищает от опечаток (см.
  стандарт FCI: только два пола для классификации).
- date_of_birth — date, не datetime: точное время рождения не
  важно, но дата нужна для определения класса на дату выставки.
- kennel_id nullable — собака может быть не привязана к питомнику
  (привозная, без документов о происхождении).
- breed_id обязательный — без породы собака не классифицируется
  в выставочной системе.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import (
    Date,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class SexEnum(str, enum.Enum):
    male = "male"
    female = "female"


class Dog(Base, TimestampMixin):
    __tablename__ = "dogs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # kennel — текущий питомник владельца. На SET NULL, чтобы при удалении
    # питомника собака не пропала: владелец мог продать её, а в БД она
    # остаётся как историческая запись.
    kennel_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("kennels.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Владелец карточки — пользователь, добавивший собаку. Прямой FK
    # dog → user: раньше владелец выводился только через kennel.owner_id,
    # и собаки без питомника оставались «ничьими» (нельзя надёжно отдать
    # «мои собаки» и проверить запись на выставку). SET NULL — собака
    # переживает удаление пользователя как историческая запись.
    # nullable — для легаси-данных, которые не удалось сопоставить.
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    breed_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        # RESTRICT на породу: пока есть собаки, породу удалять нельзя.
        # Защита от каскадного сноса справочника.
        ForeignKey("breeds.id", ondelete="RESTRICT"),
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), index=True)
    sex: Mapped[SexEnum] = mapped_column(SAEnum(SexEnum, name="sexenum"))
    date_of_birth: Mapped[date | None] = mapped_column(Date, nullable=True)
    color: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Регистрационный номер РКФ. UNIQUE среди собак — формальный
    # идентификатор в системе РКФ. nullable=True, потому что у молодых
    # щенков его может ещё не быть.
    rkf_number: Mapped[str | None] = mapped_column(
        String(64), unique=True, nullable=True
    )
    # Клеймо — идентификатор, проставленный заводчиком при рождении.
    # Обычно тоже уникален, но не у всех есть.
    tattoo: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Микрочип — стандарт ISO, 15-значный.
    microchip: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Питомник-заводчик: где собака рождена. Отличается от kennel_id
    # (текущий питомник владельца), не меняется при продаже. Источник
    # графы «Заводчик»/«Питомник» в документах.
    breeder_kennel_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("kennels.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Free-text заводчик для собак, рождённых вне платформы (импорт):
    # когда breeder_kennel_id неизвестен.
    breeder_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Самореферентные FK для родословной. SET NULL — если отца удалили
    # из БД, ребёнок остаётся с пустым полем (родословная неизвестна),
    # а не каскадно удаляется.
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

    # Помёт, из которого эта собака (этап 18). SET NULL — собака переживает
    # удаление помёта. index — под фильтр GET /dogs?litter_id= и
    # GET /litters/{id}/puppies.
    litter_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("litters.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    kennel: Mapped["Kennel | None"] = relationship(
        back_populates="dogs", foreign_keys=[kennel_id]
    )  # noqa: F821
    breeder_kennel: Mapped["Kennel | None"] = relationship(  # noqa: F821
        foreign_keys=[breeder_kennel_id]
    )
    # Используем remote_side для self-ref relationship — иначе SQLAlchemy
    # не понимает, какая сторона "родитель", какая "ребёнок".
    father: Mapped["Dog | None"] = relationship(
        "Dog", remote_side="Dog.id", foreign_keys=[father_id]
    )
    mother: Mapped["Dog | None"] = relationship(
        "Dog", remote_side="Dog.id", foreign_keys=[mother_id]
    )

    photos: Mapped[list["DogPhoto"]] = relationship(
        back_populates="dog",
        cascade="all, delete-orphan",
        order_by="DogPhoto.position",
    )


class DogPhoto(Base):
    """
    Связь dog ↔ file для фотографий.

    Это многие-ко-многим только формально: на практике один файл редко
    бывает фото нескольких собак. Но отдельная таблица даёт нам:
    - порядок отображения фото (position),
    - возможность пометить главное фото (is_primary).
    """

    __tablename__ = "dog_photos"
    __table_args__ = (
        UniqueConstraint("dog_id", "file_id", name="uq_dog_photo"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    dog_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dogs.id", ondelete="CASCADE"),
        index=True,
    )
    file_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        # CASCADE — если файл удалён из хранилища и записи в files,
        # сама связь должна тоже исчезнуть, иначе появятся "висячие"
        # ссылки на удалённые файлы.
        ForeignKey("files.id", ondelete="CASCADE"),
        index=True,
    )
    position: Mapped[int] = mapped_column(default=0)
    is_primary: Mapped[bool] = mapped_column(default=False)

    dog: Mapped["Dog"] = relationship(back_populates="photos")


class DogDocumentKind(str, enum.Enum):
    vet_passport = "vet_passport"                 # ветпаспорт (прививка от бешенства)
    pedigree = "pedigree"                         # родословная
    puppy_card = "puppy_card"                     # щенячья карточка / метрика
    working_certificate = "working_certificate"   # рабочий сертификат (рабочий класс)
    other = "other"


class DogDocument(Base):
    """
    Документ собаки (скан) для допуска на выставки.

    Документы принадлежат СОБАКЕ и переиспользуются между выставками;
    решение о допуске — отметки на конкретной записи (entry_checks).
    Одного вида может быть несколько (новый ветпаспорт после
    ревакцинации) — действующий = последний по created_at.

    valid_until — для vet_passport срок действия прививки от бешенства
    (вводит владелец при загрузке); для остальных видов обычно NULL.
    Файл (files.is_public=False) — ПДн и ветданные, публично не отдаётся.
    """

    __tablename__ = "dog_documents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    dog_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dogs.id", ondelete="CASCADE"), index=True
    )
    file_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("files.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[DogDocumentKind] = mapped_column(
        SAEnum(DogDocumentKind, name="dogdocumentkind")
    )
    valid_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
