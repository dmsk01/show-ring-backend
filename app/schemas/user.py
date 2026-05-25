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


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str


class RefreshRequest(BaseModel):
    refresh_token: str
