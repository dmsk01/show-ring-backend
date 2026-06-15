from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Enum as SAEnum,
    UniqueConstraint,
    Index,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID

from app.models.base import Base, TimestampMixin


class RoleEnum(str, enum.Enum):
    admin = "admin"
    organizer = "organizer"
    breeder = "breeder"
    judge = "judge"
    buyer = "buyer"
    # operator — оператор онлайн-поддержки (этап 11). Раздельная роль
    # от admin: модераторам поддержки не нужны полные admin-права
    # (управление пользователями, бюджетом рекламы и т.д.).
    operator = "operator"


class User(Base, TimestampMixin):
    __tablename__ = "users"
    # Phone-OTP: у пользователя обязан быть хотя бы один идентификатор —
    # email (классическая регистрация) или phone (вход по SMS-коду).
    __table_args__ = (
        CheckConstraint(
            "email IS NOT NULL OR phone IS NOT NULL",
            name="ck_users_email_or_phone",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Phone-OTP: email стал nullable — телефонные пользователи живут без него.
    email: Mapped[str | None] = mapped_column(
        String(255), unique=True, index=True, nullable=True
    )
    # E.164: "+" и до 15 цифр → 16 символов достаточно.
    phone: Mapped[str | None] = mapped_column(
        String(16), unique=True, index=True, nullable=True
    )
    # Этап 19: новый email, ожидающий подтверждения по ссылке. В email
    # выше попадает только после клика (POST /auth/confirm-email-change).
    # Пока заполнен — старый email остаётся рабочим. NULL = смена не идёт.
    pending_email: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    hashed_password: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(default=True)
    is_email_verified: Mapped[bool] = mapped_column(default=False)
    # Телефон подтверждён вводом OTP-кода (основной способ верификации,
    # см. otp_auth.verify_otp_code). Вместе с is_email_verified образует
    # сигнал доверия для тиров загрузки файлов (upload_quota).
    is_phone_verified: Mapped[bool] = mapped_column(
        default=False, server_default="false"
    )
    # Этап 4: переход с String-плейсхолдера на реальный FK → files.id.
    # SET NULL — если аватар удалён из хранилища, юзер остаётся без
    # аватара, а не "ломается" с висячей ссылкой.
    avatar_file_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("files.id", ondelete="SET NULL"),
        nullable=True,
    )

    roles: Mapped[list["UserRole"]] = relationship(
        back_populates="user",
        foreign_keys="UserRole.user_id",
        cascade="all, delete-orphan",
    )
    refresh_tokens: Mapped[list["RefreshToken"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    email_verification_tokens: Mapped[list["EmailVerificationToken"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    profile: Mapped["UserProfile | None"] = relationship(
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
    )


class UserRole(Base):
    __tablename__ = "user_roles"
    __table_args__ = (
        UniqueConstraint("user_id", "role", name="uq_user_role"),
        Index("ix_user_roles_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
    )
    role: Mapped[RoleEnum] = mapped_column(SAEnum(RoleEnum, name="roleenum"))
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    granted_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
    )

    user: Mapped["User"] = relationship(back_populates="roles", foreign_keys=[user_id])


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    # ИСПРАВЛЕНО: убран дублирующий Index("ix_refresh_tokens_token_hash") —
    # unique=True на token_hash уже создаёт unique index. Двойной индекс
    # давал лишнюю работу на INSERT без выигрыша на чтении.
    __table_args__ = (
        Index("ix_refresh_tokens_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    is_revoked: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="refresh_tokens")


class EmailVerificationToken(Base):
    __tablename__ = "email_verification_tokens"

    # Аудит L2: одна таблица обслуживает две операции — подтверждение
    # регистрации и подтверждение смены email. purpose строго разделяет их,
    # чтобы токен одной операции нельзя было предъявить в эндпоинте другой.
    PURPOSE_VERIFY = "verify"
    PURPOSE_EMAIL_CHANGE = "email_change"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    # purpose — verify | email_change. server_default 'verify': легаси-строки
    # (созданные до миграции) трактуем как регистрационные.
    purpose: Mapped[str] = mapped_column(
        String(32), default=PURPOSE_VERIFY, server_default=PURPOSE_VERIFY
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="email_verification_tokens")


class UserProfile(Base, TimestampMixin):
    """
    Профиль пользователя с человекочитаемыми данными (ФИО, страна).

    Вынесен в отдельную таблицу 1:1, чтобы не раздувать users (модель
    аутентификации) и заполнять опционально. Нужен для официальных
    документов: ФИО владельца/заводчика/эксперта и страна эксперта.
    """

    __tablename__ = "user_profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    last_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    patronymic: Mapped[str | None] = mapped_column(String(128), nullable=True)
    country: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Ссылки на соцсети (фронт /dashboard/profile/socials). Набор сетей
    # подобран под аудиторию РКФ: VK и Telegram — основные в РФ, Instagram
    # и Facebook — для международных заводчиков. Храним полный URL строкой
    # (валидация http/https в схеме UserSocialsUpdate). NULL = не указано.
    instagram: Mapped[str | None] = mapped_column(String(255), nullable=True)
    facebook: Mapped[str | None] = mapped_column(String(255), nullable=True)
    vk: Mapped[str | None] = mapped_column(String(255), nullable=True)
    telegram: Mapped[str | None] = mapped_column(String(255), nullable=True)

    user: Mapped["User"] = relationship(back_populates="profile")
