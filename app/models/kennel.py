"""
Модель питомника (этап 4).

Питомник — это страница заводчика: название, заводская приставка,
описание, город, контакты, фото-аватар. Принадлежит одному пользователю
(owner_id). Один пользователь может владеть несколькими питомниками
(переехал, сменил породу), но обычно один.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class Kennel(Base, TimestampMixin):
    __tablename__ = "kennels"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # owner — заводчик. RESTRICT, чтобы случайно не удалить юзера и не
    # подвесить питомник без владельца. Удаление юзера должно явно
    # обрабатывать его питомники (передать или закрыть).
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), index=True)
    # Заводская приставка ("kennel prefix") — уникальная часть кличек
    # собак, разведённых в этом питомнике. Например, "От Каховки".
    # Регистрируется в РКФ/FCI и не может повторяться у двух питомников.
    # Делаем UNIQUE — этого требует регламент.
    kennel_prefix: Mapped[str | None] = mapped_column(
        String(128), unique=True, nullable=True
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    city: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    country: Mapped[str | None] = mapped_column(String(64), nullable=True)
    contact_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    contact_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    website: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # avatar — отдельная UploadedFile. SET NULL — если файл удалили,
    # питомник остаётся без аватара (а не исчезает).
    avatar_file_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("files.id", ondelete="SET NULL"),
        nullable=True,
    )
    # is_verified — статус "проверен модератором". Этап 12: ставится
    # вручную через /admin/moderation/kennels/{id}/verify. Используется
    # на фронте как зелёная галочка, защита от фейковых питомников.
    is_verified: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )

    # У Dog два FK на kennels (kennel_id — текущий питомник, breeder_kennel_id
    # — питомник-заводчик). Явно указываем foreign_keys, иначе SQLAlchemy не
    # может выбрать путь связи (AmbiguousForeignKeysError при конфигурации).
    dogs: Mapped[list["Dog"]] = relationship(  # noqa: F821
        back_populates="kennel",
        cascade="save-update",
        foreign_keys="Dog.kennel_id",
    )
