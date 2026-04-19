import uuid
from enum import Enum
from sqlalchemy import String
from sqlalchemy.orm import mapped_column, Mapped

from .base import Base, TimestampMixin


class UserRole(str, Enum):
    USER = "user"
    ADMIN = "admin"
    BREEDER = "breeder"
    SELLER = "seller"


class User(Base, TimestampMixin):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    hashed_password: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(default=True)
    role: Mapped[UserRole] = mapped_column(default=UserRole.USER)
