"""
Квоты загрузки файлов: тир пользователя и проверка лимитов.

Тир выводится из сигнала доверия (email/phone verified) и ролей.
Лимиты берутся из upload_quota_tiers (БД), счётчик квоты считается в БД —
всё независимо от Redis.
"""

from __future__ import annotations

import enum

from app.models.user import User


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
