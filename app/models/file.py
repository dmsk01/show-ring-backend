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
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

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
    # is_public — отдаётся ли файл анонимным GET /files/{id}.
    # Фото собак/аватары публичны по идее платформы (True по умолчанию).
    # Сгенерированные официальные документы (дипломы/каталоги/сертификаты
    # содержат ПДн: ФИО владельца и заводчика, чип, клеймо, дату рождения)
    # воркер помечает is_public=False — они доступны только автору задачи
    # или admin через защищённый /tasks/{id}/download, а публичный
    # /files/{id} их не отдаёт (review 2026-06-01: до этого тот же файл
    # был доступен по UUID без авторизации в обход ACL на /tasks/download).
    is_public: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Обработанные варианты изображения (превью/средний с watermark).
    # Генерируются асинхронно воркером; delete-orphan — варианты живут,
    # пока жив оригинал.
    variants: Mapped[list["FileVariant"]] = relationship(
        back_populates="file", cascade="all, delete-orphan"
    )


class FileVariant(Base):
    """
    Обработанный вариант изображения (превью, средний с водяным знаком).
    Генерируется асинхронно воркером (`process_image`) после загрузки
    оригинала; сами байты — в MinIO под s3_key. 1:N к UploadedFile.
    """

    __tablename__ = "file_variants"
    __table_args__ = (
        UniqueConstraint("file_id", "kind", name="uq_file_variant_kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    file_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        # CASCADE — варианты бессмысленны без оригинала.
        ForeignKey("files.id", ondelete="CASCADE"),
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(32))  # "thumb" / "medium"
    s3_key: Mapped[str] = mapped_column(String(512), unique=True)
    content_type: Mapped[str] = mapped_column(String(128))
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    has_watermark: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    file: Mapped["UploadedFile"] = relationship(back_populates="variants")
