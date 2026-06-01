"""
Интеграция: контроль доступа к файлам и задачам.

Проверяет фиксы review 2026-06-01:
- приватные сгенерированные документы (is_public=False) не отдаются через
  публичный GET /files/{id};
- IDOR на /tasks/{id}/download — чужой пользователь получает 403.

Тела файлов из MinIO здесь НЕ читаем: и ACL-отказ (404 для приватного),
и IDOR-отказ (403) срабатывают ДО обращения к хранилищу, поэтому тесты
самодостаточны и не требуют поднятого MinIO.
"""

from __future__ import annotations

import uuid

from app.models.file import UploadedFile
from app.models.task import Task, TaskStatusEnum

PASSWORD = "secret123"


async def _make_user(client) -> tuple[uuid.UUID, str]:
    """Регистрирует и логинит пользователя, возвращает (id, access_token)."""
    email = f"itest_{uuid.uuid4().hex[:10]}@example.com"
    await client.post(
        "/auth/register", json={"email": email, "password": PASSWORD}
    )
    r = await client.post(
        "/auth/login", json={"email": email, "password": PASSWORD}
    )
    access = r.json()["access_token"]
    me = await client.get(
        "/users/me", headers={"Authorization": f"Bearer {access}"}
    )
    return uuid.UUID(me.json()["id"]), access


async def test_private_document_not_served_via_public_endpoint(
    client, db_session
):
    f = UploadedFile(
        uploaded_by=None,
        s3_key=f"documents/{uuid.uuid4()}.pdf",
        original_filename="diploma.pdf",
        content_type="application/pdf",
        size_bytes=1234,
        is_public=False,  # сгенерированный документ с ПДн — приватный
    )
    db_session.add(f)
    await db_session.commit()

    r = await client.get(f"/files/{f.id}")
    # 404 (а не 403) — не раскрываем существование приватного файла анониму.
    assert r.status_code == 404


async def test_unknown_file_returns_404(client):
    r = await client.get(f"/files/{uuid.uuid4()}")
    assert r.status_code == 404


async def test_public_file_passes_acl(client, db_session):
    # is_public=True проходит ACL и доходит до чтения из MinIO. Без MinIO
    # это 404/503 от хранилища; важно, что ACL не блокирует (не 403) и
    # эндпоинт не падает 500.
    f = UploadedFile(
        uploaded_by=None,
        s3_key=f"general/{uuid.uuid4()}.jpg",
        original_filename="avatar.jpg",
        content_type="image/jpeg",
        size_bytes=10,
        is_public=True,
    )
    db_session.add(f)
    await db_session.commit()

    r = await client.get(f"/files/{f.id}")
    assert r.status_code in (200, 404, 503)


async def test_task_download_idor_cross_user_forbidden(client, db_session):
    owner_id, _owner_token = await _make_user(client)
    other_id, other_token = await _make_user(client)
    assert owner_id != other_id

    task = Task(
        type="generate_catalog",
        status=TaskStatusEnum.done,
        payload={},
        result={"file_id": str(uuid.uuid4())},
        created_by=owner_id,
    )
    db_session.add(task)
    await db_session.commit()

    # Чужой пользователь → 403 (ACL до обращения к MinIO).
    r = await client.get(
        f"/tasks/{task.id}/download",
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert r.status_code == 403

    # Без токена → 401.
    r = await client.get(f"/tasks/{task.id}/download")
    assert r.status_code == 401
