"""
ORM-модели справочников (этап 3).

Архитектура мультивидовая: породы, классы, титулы и оценки привязаны
к animal_type. Это позволяет на старте поддерживать собак (РКФ/FCI),
а позже подключать кошек или других животных без миграции схемы —
достаточно нового набора строк в справочниках.

Ключевые решения:
- UUID-первичные ключи (как и у users) — упрощают слияние данных
  между средами (dev/stage/prod) и не лочат insert-горячую точку,
  как авто-инкремент.
- Уникальность по (animal_type_id, code) вместо глобальной — один и
  тот же код "open" может использоваться и у собак, и у кошек.
- breed_group_id nullable — позволяет хранить породы без официальной
  привязки к группе FCI (например, не признанные FCI).
- age_to_months nullable — "без верхней границы" (открытый класс,
  класс чемпионов и ветеранов).
- ondelete="RESTRICT" на справочных FK (например, breed → group) —
  нельзя удалить запись справочника, если на неё ссылаются другие
  записи. Защищает от случайного каскадного сноса данных.
"""

from __future__ import annotations

import uuid

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class AnimalType(Base, TimestampMixin):
    """
    Вид животного: dog, cat и т.д.

    Это корень мультивидовой иерархии — все остальные справочники
    привязаны к animal_type, чтобы один и тот же "open class" мог
    иметь разные правила для собак и для кошек.
    """

    __tablename__ = "animal_types"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # code — машинный идентификатор ("dog"), name — человекочитаемое имя.
    # Разделяем, чтобы в API/коде можно было полагаться на стабильный код,
    # а отображаемое имя локализовать без миграции.
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    # name — русский (канонический), name_en — английский перевод.
    # Отдача по Accept-Language с фолбэком на name, поэтому name_en nullable.
    name: Mapped[str] = mapped_column(String(128))
    name_en: Mapped[str | None] = mapped_column(String(128), nullable=True)

    breed_groups: Mapped[list["BreedGroup"]] = relationship(
        back_populates="animal_type", cascade="all, delete-orphan"
    )
    breeds: Mapped[list["Breed"]] = relationship(
        back_populates="animal_type", cascade="all, delete-orphan"
    )


class BreedGroup(Base, TimestampMixin):
    """
    Группа пород (FCI 1..10 для собак).

    Группы FCI задают зоотехническую классификацию (1 — овчарки и
    скотогонные, 2 — пинчеры/шнауцеры и т.д.). У других видов
    животных могут быть свои группировки — отсюда привязка к animal_type.
    """

    __tablename__ = "breed_groups"
    __table_args__ = (
        # У одного вида номер группы и код уникальны. Между разными
        # видами совпадения допустимы (у кошек тоже может быть "группа 1").
        UniqueConstraint("animal_type_id", "number", name="uq_breed_group_number"),
        UniqueConstraint("animal_type_id", "code", name="uq_breed_group_code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    animal_type_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        # RESTRICT — нельзя удалить вид животных, пока есть группы.
        # CASCADE здесь рискован: одна ошибочная команда удалит весь
        # набор пород и групп.
        ForeignKey("animal_types.id", ondelete="RESTRICT"),
        index=True,
    )
    number: Mapped[int] = mapped_column(Integer)
    code: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(255))
    name_en: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    description_en: Mapped[str | None] = mapped_column(Text, nullable=True)

    animal_type: Mapped["AnimalType"] = relationship(back_populates="breed_groups")
    breeds: Mapped[list["Breed"]] = relationship(back_populates="group")


class Breed(Base, TimestampMixin):
    """
    Порода (350+ для собак FCI).

    Привязана к animal_type обязательно (порода ВСЕГДА относится к виду)
    и к breed_group опционально (есть породы вне официальных групп FCI).
    """

    __tablename__ = "breeds"
    __table_args__ = (
        # Уникальный код в пределах вида. Так "labrador" — это собака,
        # а у кошек никогда не будет конфликта.
        UniqueConstraint("animal_type_id", "code", name="uq_breed_code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    animal_type_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("animal_types.id", ondelete="RESTRICT"),
        index=True,
    )
    breed_group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        # SET NULL — если группа удалена (а собак нет), породы не
        # пропадают, а становятся "без группы". RESTRICT для каскада
        # делает sevice.delete_breed_group() более явным.
        ForeignKey("breed_groups.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    code: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(255), index=True)
    name_en: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Номер стандарта FCI — строка, потому что встречаются варианты
    # "122", "122a", "344". Не nullable=False, потому что у не-FCI пород
    # его нет.
    fci_number: Mapped[str | None] = mapped_column(String(16), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    description_en: Mapped[str | None] = mapped_column(Text, nullable=True)

    animal_type: Mapped["AnimalType"] = relationship(back_populates="breeds")
    group: Mapped["BreedGroup | None"] = relationship(back_populates="breeds")


class ShowClass(Base, TimestampMixin):
    """
    Выставочный класс (бэби, щенки, юниоры, открытый, ветераны...).

    Возраст хранится в МЕСЯЦАХ (а не в годах), потому что у щенячьих
    классов границы — 4–6, 6–9, 9–18 месяцев. В одной единице хранить
    проще и точнее: возраст собаки на дату выставки = дней / 30.
    age_to_months=NULL означает "без верхней границы" (открытый класс,
    класс чемпионов).
    """

    __tablename__ = "show_classes"
    __table_args__ = (
        UniqueConstraint("animal_type_id", "code", name="uq_show_class_code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    animal_type_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("animal_types.id", ondelete="RESTRICT"),
        index=True,
    )
    code: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128))
    name_en: Mapped[str | None] = mapped_column(String(128), nullable=True)
    age_from_months: Mapped[int] = mapped_column(Integer)
    age_to_months: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # На этапе 7 (правила РКФ) логика выдачи титулов опирается на этот
    # флаг: в бэби/щенках/юниорах не выдаётся САС, в открытом/рабочем —
    # выдаётся. Храним явно, чтобы не зашивать список классов в код.
    can_receive_cac: Mapped[bool] = mapped_column(Boolean, default=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    description_en: Mapped[str | None] = mapped_column(Text, nullable=True)

    animal_type: Mapped["AnimalType"] = relationship()


class ShowRank(Base, TimestampMixin):
    """
    Ранг выставки: CACIB, CAC ЧРКФ ОС, CAC ЧРКФ, КЧК, ПК, ЧК...

    Ранг определяет, какие максимальные титулы могут быть присвоены.
    Не привязан к animal_type — у РКФ ранги одинаковы для всех видов,
    которые проводятся под их эгидой. Если в будущем для кошек будут
    свои ранги — добавим nullable FK.
    """

    __tablename__ = "show_ranks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    name_en: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    description_en: Mapped[str | None] = mapped_column(Text, nullable=True)


class Title(Base, TimestampMixin):
    """
    Титул: CW, CAC, R.CAC, CACIB, R.CACIB, BOB, BIG, BIS, ЮСАС, ...

    is_reserve=True для резервных титулов (R.CAC, R.CACIB) — на этапе 7
    логика начисления и пересчёта при дисквалификации основного
    использует этот флаг, чтобы не сравнивать строки.
    """

    __tablename__ = "titles"
    __table_args__ = (
        UniqueConstraint("animal_type_id", "code", name="uq_title_code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    animal_type_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("animal_types.id", ondelete="RESTRICT"),
        index=True,
    )
    code: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128))
    name_en: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_reserve: Mapped[bool] = mapped_column(Boolean, default=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    description_en: Mapped[str | None] = mapped_column(Text, nullable=True)

    animal_type: Mapped["AnimalType"] = relationship()


class Grade(Base, TimestampMixin):
    """
    Оценка эксперта: отлично, очень хорошо, хорошо, удовлетворительно,
    дисквалификация; щенячьи: большая перспектива, перспективный и т.д.

    Два флага:
    - is_disqualifying — оценка не позволяет дальше участвовать (дисквалификация).
      На этапе 7 такие оценки автоматически снимают собаку с дальнейшего
      бонитирования.
    - is_puppy_grade — оценка применима к щенячьим классам (бэби/щенки/юниоры).
      Это валидирует, что эксперт не выставит "большую перспективу"
      взрослой собаке в открытом классе.
    """

    __tablename__ = "grades"
    __table_args__ = (
        UniqueConstraint("animal_type_id", "code", name="uq_grade_code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    animal_type_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("animal_types.id", ondelete="RESTRICT"),
        index=True,
    )
    code: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128))
    name_en: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_disqualifying: Mapped[bool] = mapped_column(Boolean, default=False)
    is_puppy_grade: Mapped[bool] = mapped_column(Boolean, default=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    description_en: Mapped[str | None] = mapped_column(Text, nullable=True)

    animal_type: Mapped["AnimalType"] = relationship()
