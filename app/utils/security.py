import asyncio
from datetime import datetime, timedelta, timezone
import secrets
import hashlib
import bcrypt
import jwt

from app.config import settings

# Ревью 2026-10-06, BE-29: python-jose и passlib не поддерживаются (passlib
# держал bcrypt<4.1). Теперь PyJWT и bcrypt напрямую; хэши $2b$ те же —
# миграция паролей не нужна. JWTError — единый тип ошибки токена для
# вызывающего кода (раньше jose.JWTError).
JWTError = jwt.PyJWTError

# bcrypt хэширует только первые 72 байта. passlib молча обрезал длиннее —
# так же поступаем и мы, чтобы старые хэши таких паролей проверялись
# (новые пароли >72 байт отсекает validate_password).
_BCRYPT_MAX_BYTES = 72


def _bcrypt_bytes(plain: str) -> bytes:
    return plain.encode("utf-8")[:_BCRYPT_MAX_BYTES]

SECRET_KEY = settings.secret_key
ALGORITHM = "HS256"

# ИСПРАВЛЕНО: фиксированный bcrypt-хеш для constant-time проверки в login,
# когда пользователь не найден. Без него длительность ответа выдавала
# существование email (timing attack → user enumeration).
#
# ИСПРАВЛЕНО (review 2026-05-28): раньше хэш вычислялся на
# импорте модуля и стоил ~250 мс CPU. Этот файл импортируется из всего
# app (через app.dependencies, app.services.auth), и налог платился
# каждым процессом API/worker'а на холодном старте + каждым pytest-
# процессом. Теперь хеш кешируется лениво при первом вызове
# dummy_verify_password — в проде он прогревается первым же логином
# несуществующего пользователя, в тестах — соответствующим тестом.
_DUMMY_BCRYPT_HASH: str | None = None


# Группа 1 — Пароли
def hash_password(plain: str) -> str:
    return bcrypt.hashpw(_bcrypt_bytes(plain), bcrypt.gensalt()).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(_bcrypt_bytes(plain), hashed.encode("ascii"))
    except ValueError:
        # Битый/неизвестный формат хэша — не совпадение, а не 500.
        return False


def dummy_verify_password() -> None:
    # ИСПРАВЛЕНО: вызывается, когда юзер не найден, чтобы выровнять
    # время ответа с реальной bcrypt-верификацией.
    global _DUMMY_BCRYPT_HASH
    if _DUMMY_BCRYPT_HASH is None:
        _DUMMY_BCRYPT_HASH = hash_password("dummy-password-for-timing")
    verify_password("dummy-password-for-timing", _DUMMY_BCRYPT_HASH)


# Async-обёртки (ревью 2026-10-06, BE-08): bcrypt — сотни миллисекунд чистого
# CPU. Синхронный вызов из async-обработчика замораживал весь процесс
# uvicorn (другие запросы, WebSocket) на время каждой проверки пароля.
# В async-коде вызываем только эти обёртки — работа уходит в thread pool
# (bcrypt отпускает GIL, потоки действительно параллельны).
async def hash_password_async(plain: str) -> str:
    return await asyncio.to_thread(hash_password, plain)


async def verify_password_async(plain: str, hashed: str) -> bool:
    return await asyncio.to_thread(verify_password, plain, hashed)


async def dummy_verify_password_async() -> None:
    await asyncio.to_thread(dummy_verify_password)


def validate_password(password: str) -> None:
    if not (8 <= len(password) <= 128):
        raise ValueError("Пароль должен содержать от 8 до 128 символов.")
    # ИСПРАВЛЕНО (review 2026-06-10): bcrypt хеширует только первые
    # 72 байта — всё дальше молча не влияло на проверку (для UTF-8
    # кириллицы порог наступает уже на ~36 символах). Отклоняем на
    # валидации, а не пре-хешируем (sha256→bcrypt): пре-хеш потребовал
    # бы миграции существующих хешей.
    if len(password.encode("utf-8")) > 72:
        raise ValueError(
            "Пароль не должен быть длиннее 72 байт в UTF-8 "
            "(ограничение bcrypt)."
        )


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
            "require": ["exp", "sub"],
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
