import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, field_validator

from app.models.user import RoleEnum
from app.utils.security import validate_password


class UserCreate(BaseModel):
    email: EmailStr
    password: str

    @field_validator("password")
    @classmethod
    def validate_pwd(cls, v: str) -> str:
        validate_password(v)
        return v


class RoleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    role: RoleEnum
    granted_at: datetime


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    is_active: bool
    is_email_verified: bool
    roles: list[RoleResponse]
    created_at: datetime


# ИСПРАВЛЕНО: отдельная схема для публичного GET /users/{id} — без email
# и без флага is_email_verified, чтобы не раскрывать PII неавторизованным.
class PublicUserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    is_active: bool
    roles: list[RoleResponse]
    created_at: datetime


class UserUpdate(BaseModel):
    email: EmailStr | None = None
    # ИСПРАВЛЕНО (bug_203): смена email — sensitive операция. Без re-auth
    # компрометация access-токена даёт атакующему смену email на свой и
    # последующий захват аккаунта через password reset. Текущий пароль
    # обязателен только когда меняется email (роутер валидирует это
    # отдельно — Pydantic-валидатор не имеет доступа к current_user).
    current_password: str | None = None


class PasswordChange(BaseModel):
    # Этап 19: смена пароля авторизованным пользователем. current_password
    # для re-auth (украденный access-токен без знания пароля не сменит
    # его). new_password проходит ту же политику, что и регистрация.
    current_password: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def validate_new_pwd(cls, v: str) -> str:
        validate_password(v)
        return v


class EmailChangeConfirm(BaseModel):
    # Токен из письма подтверждения смены email.
    token: str


class ResendVerification(BaseModel):
    # Повторная отправка письма подтверждения регистрации. Принимаем
    # email (не current_user), чтобы работало и для незалогиненных.
    # Ответ одинаков независимо от существования адреса (анти-enumeration).
    email: EmailStr


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str


class RefreshRequest(BaseModel):
    refresh_token: str


class UserProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    last_name: str | None = None
    first_name: str | None = None
    patronymic: str | None = None
    country: str | None = None


class UserProfileUpdate(BaseModel):
    last_name: str | None = None
    first_name: str | None = None
    patronymic: str | None = None
    country: str | None = None
