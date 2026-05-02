from uuid import UUID
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import EmailVerificationToken, RefreshToken, User


async def get_user_by_email(db: AsyncSession, email: str) -> User | None:
    stmt = select(User).where(User.email == email).options(selectinload(User.roles))

    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def get_user_by_id(db: AsyncSession, user_id: UUID) -> User | None:
    stmt = select(User).where(User.id == user_id).options(selectinload(User.roles))

    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def create_user(db: AsyncSession, email: str, hashed_password: str) -> User:
    user = User(hashed_password=hashed_password, email=email)
    db.add(user)
    await db.flush()
    return user


async def update_user(db: AsyncSession, user: User, **fields) -> User:
    for field, value in fields.items():
        setattr(user, field, value)
    await db.flush()
    return user


async def create_refresh_token(
    db: AsyncSession, user_id: UUID, token_hash: str, expires_at: datetime
) -> RefreshToken:
    token = RefreshToken(user_id=user_id, token_hash=token_hash, expires_at=expires_at)

    db.add(token)
    await db.flush()
    return token


async def get_refresh_token_by_hash(
    db: AsyncSession, token_hash: str
) -> RefreshToken | None:
    stmt = (
        select(RefreshToken)
        .where(RefreshToken.token_hash == token_hash)
        .options(selectinload(RefreshToken.user).selectinload(User.roles))
    )

    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def revoke_refresh_token(db: AsyncSession, token_hash: str) -> None:
    stmt = (
        update(RefreshToken)
        .where(RefreshToken.token_hash == token_hash)
        .values(is_revoked=True)
    )

    await db.execute(stmt)


async def create_email_verification_token(
    db: AsyncSession, user_id: UUID, token_hash: str, expires_at: datetime
) -> EmailVerificationToken:
    token = EmailVerificationToken(user_id=user_id, token_hash=token_hash, expires_at=expires_at)

    db.add(token)
    await db.flush()
    return token


async def get_email_verification_token_by_hash(
    db: AsyncSession, token_hash: str
) -> EmailVerificationToken | None:
    stmt = (
        select(EmailVerificationToken)
        .where(EmailVerificationToken.token_hash == token_hash)
    )

    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def mark_email_token_used(db: AsyncSession, token_hash: str) -> None:
    stmt = (
        update(EmailVerificationToken)
        .where(EmailVerificationToken.token_hash == token_hash)
        .values(used_at=datetime.now(timezone.utc))
    )

    await db.execute(stmt)


async def set_user_email_verified(db: AsyncSession, user_id: UUID) -> None:
    stmt = update(User).where(User.id == user_id).values(is_email_verified=True)

    await db.execute(stmt)
