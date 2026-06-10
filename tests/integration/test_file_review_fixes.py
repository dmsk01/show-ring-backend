"""
Интеграция: правки по review 2026-06-10 — файлы.

- folder при загрузке валидируется (^[a-z0-9_-]{1,32}$): раньше сырая
  query-строка попадала в S3-ключ — можно было класть файлы в чужие
  префиксы (documents/) или ронять 500 строкой длиннее 512.
- Эндпоинты вариантов изображения уважают is_public исходного файла:
  раньше приватный image был бы доступен анонимно через варианты
  (латентный обход ACL).
"""

from __future__ import annotations

import uuid

import pytest

from app.models.file import FileVariant, UploadedFile

PASSWORD = "secret123"


async def _make_token(client) -> str:
    email = f"file_{uuid.uuid4().hex[:10]}@example.com"
    await client.post(
        "/auth/register", json={"email": email, "password": PASSWORD}
    )
    r = await client.post(
        "/auth/login", json={"email": email, "password": PASSWORD}
    )
    return r.json()["access_token"]


async def _private_file_with_variant(db_session) -> tuple[UploadedFile, FileVariant]:
    f = UploadedFile(
        s3_key=f"documents/{uuid.uuid4()}.png",
        original_filename="scan.png",
        content_type="image/png",
        size_bytes=10,
        is_public=False,
    )
    db_session.add(f)
    await db_session.commit()
    v = FileVariant(
        file_id=f.id,
        kind="thumb",
        s3_key=f"variants/{uuid.uuid4()}.png",
        content_type="image/png",
        width=100,
        height=100,
        size_bytes=5,
    )
    db_session.add(v)
    await db_session.commit()
    return f, v


async def test_upload_rejects_bad_folder(client):
    """folder вне белого списка отклоняется (422), до похода в S3.

    documents — зарезервированный префикс приватных сгенерированных
    документов, пользовательская загрузка туда запрещена.
    """
    token = await _make_token(client)
    for bad in ("Documents/../{evil}", "documents", "variants", "x" * 600):
        r = await client.post(
            "/files/upload",
            params={"folder": bad},
            files={"file": ("a.png", b"\x89PNG\r\n\x1a\n0000", "image/png")},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 422, f"folder={bad!r} прошёл валидацию"


async def test_variants_list_hidden_for_private_file(client, db_session):
    """Список вариантов приватного файла → 404 (та же семантика, что /files/{id})."""
    f, _v = await _private_file_with_variant(db_session)
    r = await client.get(f"/files/{f.id}/variants")
    assert r.status_code == 404


async def test_variant_download_hidden_for_private_file(
    client, db_session, monkeypatch
):
    """Скачивание варианта приватного файла → 404.

    get_file_stream подменяем: иначе 404 пришёл бы из-за отсутствия
    объекта в MinIO, и тест «проходил» бы и без ACL-проверки.
    """
    async def _fake_stream(s3_key: str):
        return b"img-bytes", "image/png"

    monkeypatch.setattr(
        "app.routers.files.file_storage.get_file_stream", _fake_stream
    )
    _f, v = await _private_file_with_variant(db_session)
    r = await client.get(f"/files/variants/{v.id}")
    assert r.status_code == 404
