"""
Запросы для квот загрузки: агрегации по files, чтение/запись лимитов
upload_quota_tiers, проверка владения питомником.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.file import UploadedFile
from app.models.kennel import Kennel
from app.models.upload_quota import UploadQuotaTier


async def count_uploads_since(
    db: AsyncSession, user_id: UUID, since: datetime
) -> int:
    """Сколько файлов юзер загрузил с момента `since` (скользящее окно)."""
    stmt = (
        select(func.count())
        .select_from(UploadedFile)
        .where(
            UploadedFile.uploaded_by == user_id,
            UploadedFile.created_at > since,
        )
    )
    return int(await db.scalar(stmt) or 0)


async def oldest_upload_since(
    db: AsyncSession, user_id: UUID, since: datetime
) -> datetime | None:
    """created_at самой старой загрузки в окне — для расчёта cooldown."""
    stmt = select(func.min(UploadedFile.created_at)).where(
        UploadedFile.uploaded_by == user_id,
        UploadedFile.created_at > since,
    )
    return await db.scalar(stmt)


async def sum_user_storage_bytes(db: AsyncSession, user_id: UUID) -> int:
    """Суммарный объём всех файлов юзера (без окна — «сколько занимает»)."""
    stmt = select(
        func.coalesce(func.sum(UploadedFile.size_bytes), 0)
    ).where(UploadedFile.uploaded_by == user_id)
    return int(await db.scalar(stmt) or 0)


async def user_owns_kennel(db: AsyncSession, user_id: UUID) -> bool:
    """Есть ли у юзера хотя бы один питомник (признак тира breeder)."""
    stmt = select(
        select(Kennel.id).where(Kennel.owner_id == user_id).exists()
    )
    return bool(await db.scalar(stmt))


async def get_tier_config(
    db: AsyncSession, tier: str
) -> UploadQuotaTier | None:
    return await db.get(UploadQuotaTier, tier)


async def list_tier_configs(db: AsyncSession) -> list[UploadQuotaTier]:
    stmt = select(UploadQuotaTier).order_by(UploadQuotaTier.tier)
    return list((await db.execute(stmt)).scalars().all())


async def update_tier_config(
    db: AsyncSession,
    tier: str,
    daily_limit: int,
    max_storage_bytes: int,
) -> UploadQuotaTier | None:
    """Обновить лимиты тира. None — если строки нет (неизвестный тир)."""
    config = await db.get(UploadQuotaTier, tier)
    if config is None:
        return None
    config.daily_limit = daily_limit
    config.max_storage_bytes = max_storage_bytes
    await db.flush()
    return config
