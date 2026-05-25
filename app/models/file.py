"""
ORM-модели для файлового хранилища (этап 4).

Архитектура:
- Сам файл лежит в S3/MinIO под ключом `s3_key`.
- В БД храним метаданные: владелец, размер, MIME, оригинальное имя.
- Связи "что чему принадлежит" решаются на стороне сущности (avatar_file_id
  у user, kennel.avatar_file_id, dog_photos m2m). Не делаем
  полиморфных FK ("owner_type"+"owner_id") — это ломает целостность БД
  и не отличает orphan-файлы.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class UploadedFile(Base):
    """
    Метаданные загруженного файла. Сами байты — в MinIO под s3_key.

    Почему не TimestampMixin: updated_at для файла не имеет смысла —
    файл загрузили один раз, изменяются только связи с ним.
    """

    __tablename__ = "files"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # uploaded_by — кто загрузил. SET NULL на удаление пользователя:
    # сам файл (фото собаки) не должен исчезать при удалении владельца —
    # его собака осталась.
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # s3_key — путь внутри бакета. Формат "<prefix>/<uuid>.<ext>", где
    # uuid гарантирует уникальность ключа независимо от оригинального имени.
    # unique=True — защита от случайной перезаписи.
    s3_key: Mapped[str] = mapped_column(String(512), unique=True)
    # Оригинальное имя нужно при скачивании (Content-Disposition: filename=...),
    # чтобы пользователь получил знакомое название.
    original_filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
