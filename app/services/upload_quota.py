"""
Квоты загрузки файлов: тир пользователя и проверка лимитов.

Тир выводится из сигнала доверия (email/phone verified) и ролей.
Лимиты берутся из upload_quota_tiers (БД), счётчик квоты считается в БД —
всё независимо от Redis.
"""

from __future__ import annotations

import enum
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.repositories import upload_quota as repo

logger = logging.getLogger(__name__)


class UploadTier(str, enum.Enum):
    untrusted = "untrusted"
    standard = "standard"
    breeder = "breeder"


def resolve_upload_tier(user: User, *, owns_kennel: bool) -> UploadTier:
    """
    Тир пользователя для квот загрузки.

    breeder (самый свободный) — роль breeder ИЛИ владение питомником
    (создание питомника не выдаёт роль, поэтому проверяем оба признака).
    standard — верифицирован (email или телефон). untrusted — иначе.
    owns_kennel вычисляет вызывающий (repo.user_owns_kennel), чтобы
    функция оставалась чистой и юнит-тестируемой.
    """
    roles = {r.role.value for r in user.roles}
    if "breeder" in roles or owns_kennel:
        return UploadTier.breeder
    if user.is_email_verified or user.is_phone_verified:
        return UploadTier.standard
    return UploadTier.untrusted


class UploadQuotaExceeded(Exception):
    """
    Квота исчерпана. Несёт готовые status_code/body/headers — роутер
    отдаёт их как JSONResponse (нужен структурированный body с инфо о
    периоде охлаждения, чего HTTPException.detail не даёт плоско).
    """

    def __init__(self, status_code: int, body: dict, headers: dict) -> None:
        self.status_code = status_code
        self.body = body
        self.headers = headers
        super().__init__(body.get("detail", "quota exceeded"))


def _cooldown(
    oldest: datetime | None, now: datetime
) -> tuple[datetime, int]:
    """
    Когда освободится слот суточного окна и сколько до этого секунд.
    reset_at = (самая старая загрузка в окне) + 24ч; если загрузок нет —
    now + 24ч (граничный случай). retry_after не отрицателен.
    """
    reset_at = (oldest + timedelta(days=1)) if oldest else (now + timedelta(days=1))
    retry_after = max(0, int((reset_at - now).total_seconds()))
    return reset_at, retry_after


async def check_upload_quota(
    db: AsyncSession, user: User, *, declared_size_bytes: int
) -> None:
    """
    Проверить квоту перед загрузкой. Поднимает UploadQuotaExceeded при
    превышении суточной частоты (429) или объёма (413). Если конфига тира
    нет (ops-ошибка, а не атака) — fail-open с warning, чтобы не ронять
    загрузку.
    """
    owns_kennel = await repo.user_owns_kennel(db, user.id)
    tier = resolve_upload_tier(user, owns_kennel=owns_kennel)
    config = await repo.get_tier_config(db, tier.value)
    if config is None:
        logger.warning(
            "No upload quota config for tier %s — fail-open", tier.value
        )
        return

    now = datetime.now(timezone.utc)
    since = now - timedelta(days=1)

    used = await repo.count_uploads_since(db, user.id, since)
    if used >= config.daily_limit:
        oldest = await repo.oldest_upload_since(db, user.id, since)
        reset_at, retry_after = _cooldown(oldest, now)
        raise UploadQuotaExceeded(
            status_code=429,
            body={
                "detail": "Дневной лимит загрузок исчерпан",
                "tier": tier.value,
                "limit": config.daily_limit,
                "used": used,
                "retry_after_seconds": retry_after,
                "reset_at": reset_at.isoformat(),
            },
            headers={"Retry-After": str(retry_after)},
        )

    used_bytes = await repo.sum_user_storage_bytes(db, user.id)
    if used_bytes + declared_size_bytes > config.max_storage_bytes:
        raise UploadQuotaExceeded(
            status_code=413,
            body={
                "detail": "Превышен лимит общего объёма хранилища",
                "tier": tier.value,
                "max_storage_bytes": config.max_storage_bytes,
                "used_bytes": used_bytes,
            },
            headers={},
        )
