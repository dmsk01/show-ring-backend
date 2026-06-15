"""Юнит-тесты резолвера тира загрузки (чистая функция над User)."""

from __future__ import annotations

from app.models.user import RoleEnum, User, UserRole
from app.services.upload_quota import UploadTier, resolve_upload_tier


def _user(*, email_verified=False, phone_verified=False, roles=()):
    u = User(is_email_verified=email_verified, is_phone_verified=phone_verified)
    # roles=[] в конструкторе не передать через kwargs relationship на
    # detached-объекте безопасно — присваиваем явно списком.
    u.roles = [UserRole(role=r) for r in roles]
    return u


def test_unverified_no_kennel_is_untrusted():
    u = _user()
    assert resolve_upload_tier(u, owns_kennel=False) is UploadTier.untrusted


def test_email_verified_is_standard():
    u = _user(email_verified=True)
    assert resolve_upload_tier(u, owns_kennel=False) is UploadTier.standard


def test_phone_verified_is_standard():
    u = _user(phone_verified=True)
    assert resolve_upload_tier(u, owns_kennel=False) is UploadTier.standard


def test_breeder_role_is_breeder():
    u = _user(roles=(RoleEnum.breeder,))
    assert resolve_upload_tier(u, owns_kennel=False) is UploadTier.breeder


def test_kennel_owner_is_breeder_even_if_only_email_verified():
    u = _user(email_verified=True)
    assert resolve_upload_tier(u, owns_kennel=True) is UploadTier.breeder
