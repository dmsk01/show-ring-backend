from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession
from app.schemas.user import TokenResponse
from app.utils.security import (
    create_access_token,
    create_refresh_token_value,
    hash_password,
    generate_verification_token,
    hash_token,
    verify_password,
)
from app.repositories import user as user_repo


async def register_user(db: AsyncSession, email: str, password: str):
    existing = await user_repo.get_user_by_email(db, email)
    if existing:
        raise ValueError("Email уже занят")

    hashed = hash_password(password)
    user = await user_repo.create_user(db, email, hashed)

    raw_token, token_hash = generate_verification_token()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=24)

    await user_repo.create_email_verification_token(db, user.id, token_hash, expires_at)

    # заглушка отправки почты
    print(f"[DEV] Verify token: {raw_token}")
    await db.commit()
    return user


async def verify_email(db: AsyncSession, raw_token: str):
    token_hash = hash_token(raw_token)

    db_token = await user_repo.get_email_verification_token_by_hash(db, token_hash)

    if (
        not db_token
        or db_token.expires_at < datetime.now(timezone.utc)
        or db_token.used_at
    ):
        raise ValueError("Невалидный токен")

    db_token.used_at = datetime.now(timezone.utc)

    user = await user_repo.get_user_by_id(db, db_token.user_id)

    if not user:
        raise ValueError("Пользователь не найден")

    user.is_email_verified = True

    await db.commit()


async def login_user(db: AsyncSession, email: str, password: str) -> TokenResponse:
    user = await user_repo.get_user_by_email(db, email)

    if not user:
        raise ValueError("Неверный email или пароль")

    is_password_valid = verify_password(password, user.hashed_password)

    if not is_password_valid:
        raise ValueError("Неверный email или пароль")

    roles = [r.role.value for r in user.roles]

    access = create_access_token(str(user.id), roles)

    raw_refresh = create_refresh_token_value()
    refresh_hash = hash_token(raw_refresh)
    expires_at = datetime.now(timezone.utc) + timedelta(days=7)

    await user_repo.create_refresh_token(db, user.id, refresh_hash, expires_at)

    await db.commit()

    return TokenResponse(
        access_token=access, refresh_token=raw_refresh, token_type="bearer"
    )


async def refresh_access_token(db: AsyncSession, raw_refresh_token: str):
    token_hash = hash_token(raw_refresh_token)
    db_token = await user_repo.get_refresh_token_by_hash(db, token_hash)

    if not db_token:
        raise ValueError("Невалидный токен")

    user = await user_repo.get_user_by_id(db, db_token.user_id)

    if not user:
        raise ValueError("Пользователь не найден")

    roles = [r.role.value for r in user.roles]

    return create_access_token(str(user.id), roles)


async def logout_user(db: AsyncSession, raw_refresh_token: str):
    token_hash = hash_token(raw_refresh_token)

    token = await user_repo.get_refresh_token_by_hash(db, token_hash)
    if token is None:
        raise ValueError("Refresh token не найден")

    await user_repo.revoke_refresh_token(db, token_hash)
    await db.commit()
