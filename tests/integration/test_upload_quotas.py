"""
Интеграционные тесты квот загрузки файлов: сигнал доверия
is_phone_verified, суточная и объёмная квота (на уровне сервиса и HTTP),
админский CRUD лимитов.

Харнесс — реальный PostgreSQL (миграции применены, upload_quota_tiers
засеяна) + Redis, всё в транзакции с rollback (см. conftest).
"""

from __future__ import annotations

import uuid

import pytest

from app.models.file import UploadedFile
from app.models.user import RoleEnum, User, UserRole
from app.repositories.user import get_user_by_id, get_user_by_phone
from app.services import otp_auth
from app.services.upload_quota import (
    UploadQuotaExceeded,
    check_upload_quota,
)
from app.utils.security import hash_token

PASSWORD = "secret123"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _make_user(client) -> tuple[uuid.UUID, str]:
    """Регистрирует + логинит email-юзера (is_email_verified=False)."""
    email = f"uq_{uuid.uuid4().hex[:10]}@example.com"
    await client.post("/auth/register", json={"email": email, "password": PASSWORD})
    r = await client.post(
        "/auth/login",
        json={"email": email, "password": PASSWORD},
        headers={"X-Token-Delivery": "body"},
    )
    access = r.json()["access_token"]
    me = await client.get("/users/me", headers=_auth(access))
    return uuid.UUID(me.json()["id"]), access


async def _make_admin(client, db_session) -> tuple[uuid.UUID, str]:
    uid, token = await _make_user(client)
    db_session.add(UserRole(user_id=uid, role=RoleEnum.admin))
    await db_session.commit()
    return uid, token


# --- is_phone_verified -------------------------------------------------


async def test_otp_sets_is_phone_verified(db_session, test_redis):
    """Успешный verify_otp_code выставляет is_phone_verified=True."""
    phone = f"+79{uuid.uuid4().int % 10**9:09d}"
    code = "123456"
    await test_redis.set(f"otp:code:{phone}", hash_token(code))

    await otp_auth.verify_otp_code(db_session, test_redis, phone, code)

    user = await get_user_by_phone(db_session, phone)
    assert user is not None
    assert user.is_phone_verified is True


# --- квота: уровень сервиса ------------------------------------------


async def _seed_user_in_db(
    db_session, *, email_verified=False, phone_verified=False
):
    """Создаёт юзера прямо в БД и возвращает его с загруженными ролями."""
    u = User(
        email=f"q_{uuid.uuid4().hex[:8]}@example.com",
        is_email_verified=email_verified,
        is_phone_verified=phone_verified,
        roles=[],
    )
    db_session.add(u)
    await db_session.commit()
    # get_user_by_id грузит roles через selectinload — как в проде.
    loaded = await get_user_by_id(db_session, u.id)
    assert loaded is not None
    return loaded


def _file_row(user_id, size_bytes=100):
    return UploadedFile(
        uploaded_by=user_id,
        s3_key=f"general/{uuid.uuid4()}.jpg",
        original_filename="x.jpg",
        content_type="image/jpeg",
        size_bytes=size_bytes,
    )


async def test_daily_quota_raises_429_with_cooldown(db_session):
    """untrusted (лимит 5): 5 загрузок в окне → 429 с cooldown."""
    user = await _seed_user_in_db(db_session)  # untrusted
    for _ in range(5):
        db_session.add(_file_row(user.id))
    await db_session.commit()

    with pytest.raises(UploadQuotaExceeded) as exc:
        await check_upload_quota(db_session, user, declared_size_bytes=100)

    e = exc.value
    assert e.status_code == 429
    assert e.body["tier"] == "untrusted"
    assert e.body["limit"] == 5
    assert e.body["used"] >= 5
    assert e.body["retry_after_seconds"] > 0
    assert e.headers["Retry-After"] == str(e.body["retry_after_seconds"])


async def test_storage_quota_raises_413(db_session):
    """standard (объём 500 МБ): почти полный + новый файл → 413."""
    user = await _seed_user_in_db(db_session, email_verified=True)  # standard
    db_session.add(_file_row(user.id, size_bytes=524_288_000))  # 500 МБ
    await db_session.commit()

    with pytest.raises(UploadQuotaExceeded) as exc:
        await check_upload_quota(db_session, user, declared_size_bytes=1)

    assert exc.value.status_code == 413
    assert exc.value.body["tier"] == "standard"


async def test_under_quota_passes(db_session):
    """В пределах лимитов — исключения нет."""
    user = await _seed_user_in_db(db_session, email_verified=True)  # standard
    db_session.add(_file_row(user.id))
    await db_session.commit()

    # Не должно поднять исключение.
    await check_upload_quota(db_session, user, declared_size_bytes=100)


# --- квота: HTTP-эндпоинт --------------------------------------------


async def test_upload_endpoint_returns_429_when_over_quota(client, db_session):
    """6-я загрузка untrusted-юзера → 429 ДО обращения к MinIO."""
    uid, token = await _make_user(client)  # untrusted, лимит 5
    for _ in range(5):
        db_session.add(_file_row(uid))
    await db_session.commit()

    files = {"file": ("x.jpg", b"\xff\xd8\xff\xe0junkbytes", "image/jpeg")}
    r = await client.post("/files/upload", files=files, headers=_auth(token))

    assert r.status_code == 429
    body = r.json()
    assert body["tier"] == "untrusted"
    assert body["limit"] == 5
    assert body["retry_after_seconds"] > 0
    assert "retry-after" in {k.lower() for k in r.headers}


# --- админский CRUD лимитов ------------------------------------------


async def test_list_quotas_requires_admin(client):
    """Не-админ → 403."""
    _, token = await _make_user(client)
    r = await client.get("/admin/upload-quotas", headers=_auth(token))
    assert r.status_code == 403


async def test_admin_lists_quota_tiers(client, db_session):
    """Админ видит три засеянных тира."""
    _, token = await _make_admin(client, db_session)
    r = await client.get("/admin/upload-quotas", headers=_auth(token))
    assert r.status_code == 200
    tiers = {row["tier"] for row in r.json()}
    assert tiers == {"untrusted", "standard", "breeder"}


async def test_admin_updates_tier_limit(client, db_session):
    """PUT меняет лимит, новое значение видно в ответе."""
    _, token = await _make_admin(client, db_session)
    r = await client.put(
        "/admin/upload-quotas/untrusted",
        json={"daily_limit": 3, "max_storage_bytes": 10485760},
        headers=_auth(token),
    )
    assert r.status_code == 200
    assert r.json()["daily_limit"] == 3
    assert r.json()["max_storage_bytes"] == 10485760


async def test_update_unknown_tier_404(client, db_session):
    _, token = await _make_admin(client, db_session)
    r = await client.put(
        "/admin/upload-quotas/nope",
        json={"daily_limit": 3, "max_storage_bytes": 10485760},
        headers=_auth(token),
    )
    assert r.status_code == 404
