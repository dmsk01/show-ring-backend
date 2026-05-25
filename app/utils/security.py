from datetime import datetime, timedelta, timezone
import secrets
import hashlib
from passlib.context import CryptContext
from jose import jwt

from app.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

SECRET_KEY = settings.secret_key
ALGORITHM = "HS256"

# ИСПРАВЛЕНО: фиксированный bcrypt-хеш для constant-time проверки в login,
# когда пользователь не найден. Без него длительность ответа выдавала
# существование email (timing attack → user enumeration).
_DUMMY_BCRYPT_HASH = pwd_context.hash("dummy-password-for-timing")


# Группа 1 — Пароли
def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def dummy_verify_password() -> None:
    # ИСПРАВЛЕНО: вызывается, когда юзер не найден, чтобы выровнять
    # время ответа с реальной bcrypt-верификацией.
    pwd_context.verify("dummy-password-for-timing", _DUMMY_BCRYPT_HASH)


def validate_password(password: str) -> None:
    if not (8 <= len(password) <= 128):
        raise ValueError("Пароль должен содержать от 8 до 128 символов.")


# Группа 2 — JWT
def create_access_token(user_id: str, roles: list[str]) -> str:
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=settings.access_token_expire_minutes
    )
    to_encode = {"sub": user_id, "roles": roles, "type": "access", "exp": expire}
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict:
    # ИСПРАВЛЕНО: явные опции декодирования. require_exp + require_sub
    # отрезают токены без обязательных полей. verify_signature=True по
    # умолчанию, но прописываем явно для прозрачности.
    return jwt.decode(
        token,
        SECRET_KEY,
        algorithms=[ALGORITHM],
        options={
            "verify_signature": True,
            "verify_exp": True,
            "require_exp": True,
            "require_sub": True,
        },
    )


# Группа 3 — Случайные токены
def create_refresh_token_value() -> str:
    return secrets.token_hex(32)


def hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


def generate_verification_token() -> tuple[str, str]:
    raw_token = secrets.token_urlsafe(32)
    token_hash = hash_token(raw_token)
    return raw_token, token_hash
