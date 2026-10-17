"""
Интеграция: оригиналы фото — только загрузившему (ревью 2026-10-06, BE-24).

Раньше /files/{id} отдавал любому оригинал без водяного знака — фото собак
свободно скачивались в полном размере. Теперь чужим — вариант medium с
водяным знаком, пока варианта нет — 404 (оригинал не отдаётся).
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from app.models.file import FileVariant, UploadedFile
from app.models.user import RoleEnum, UserRole
from app.services import file_storage

PASSWORD = "secret123"
CONSENTS = {"accept_terms": True, "personal_data_consent": True}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _login(client, db_session, role: RoleEnum | None = None):
    email = f"wm_{uuid.uuid4().hex[:10]}@example.com"
    await client.post("/auth/register", json={"email": email, "password": PASSWORD, **CONSENTS})
    r = await client.post(
        "/auth/login",
        json={"email": email, "password": PASSWORD},
        headers={"X-Token-Delivery": "body"},
    )
    token = r.json()["access_token"]
    uid = uuid.UUID((await client.get("/users/me", headers=_auth(token))).json()["id"])
    if role is not None:
        db_session.add(UserRole(user_id=uid, role=role))
        await db_session.commit()
    return uid, token


@pytest.fixture
def served(monkeypatch) -> list[str]:
    """Какие ключи MinIO реально отдавались."""
    keys: list[str] = []

    async def _iter(key, chunk_size=64 * 1024):
        keys.append(key)
        yield b"img"

    monkeypatch.setattr(file_storage, "stat_file", AsyncMock())
    monkeypatch.setattr(file_storage, "iter_file", _iter)
    return keys


async def _photo(db_session, owner_id, *, with_variant: bool = True) -> UploadedFile:
    f = UploadedFile(
        uploaded_by=owner_id, s3_key=f"dogs/{uuid.uuid4()}.jpg",
        original_filename="rex.png", content_type="image/png",
        size_bytes=3, is_public=True,
    )
    db_session.add(f)
    await db_session.flush()
    if with_variant:
        for kind, wm in (("thumb", False), ("medium", True)):
            db_session.add(FileVariant(
                file_id=f.id, kind=kind, s3_key=f"variants/{kind}-{f.id}.jpg",
                content_type="image/jpeg", width=100, height=100,
                has_watermark=wm, size_bytes=3,
            ))
    await db_session.commit()
    return f


async def test_anonymous_gets_watermarked_variant(client, db_session, served):
    owner, _ = await _login(client, db_session)
    f = await _photo(db_session, owner)

    r = await client.get(f"/files/{f.id}")

    assert r.status_code == 200
    assert served == [f"variants/medium-{f.id}.jpg"]
    assert r.headers["content-type"] == "image/jpeg"
    assert "immutable" not in r.headers["cache-control"]
    assert "rex.jpg" in r.headers["content-disposition"]


async def test_other_user_gets_variant_too(client, db_session, served):
    owner, _ = await _login(client, db_session)
    _, stranger_token = await _login(client, db_session)
    f = await _photo(db_session, owner)

    await client.get(f"/files/{f.id}", headers=_auth(stranger_token))

    assert served == [f"variants/medium-{f.id}.jpg"]


async def test_owner_and_admin_get_original(client, db_session, served):
    owner, owner_token = await _login(client, db_session)
    _, admin_token = await _login(client, db_session, RoleEnum.admin)
    f = await _photo(db_session, owner)

    r = await client.get(f"/files/{f.id}", headers=_auth(owner_token))
    await client.get(f"/files/{f.id}", headers=_auth(admin_token))

    assert served == [f.s3_key, f.s3_key]
    assert r.headers["cache-control"].startswith("private")


async def test_no_variant_yet_is_404_not_original(client, db_session, served):
    owner, _ = await _login(client, db_session)
    f = await _photo(db_session, owner, with_variant=False)

    r = await client.get(f"/files/{f.id}")

    assert r.status_code == 404
    assert r.headers["cache-control"] == "no-store"
    assert served == []
