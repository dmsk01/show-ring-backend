# tests/integration/test_dog_documents.py
"""Интеграция: документы собаки — загрузка, список, ACL скачивания, удаление."""

from __future__ import annotations

import uuid

import pytest

import app.routers.dog_documents as router_mod
from app.models.show import ShowStaff, ShowStatus
from tests.integration.checkin_helpers import auth, make_api_user, make_world


@pytest.fixture(autouse=True)
def fake_storage(monkeypatch):
    async def _upload(file, *, folder="general"):
        await file.read()
        return f"{folder}/{uuid.uuid4()}.pdf", "application/pdf", file.filename, 4

    async def _stream(s3_key):
        return b"%PDF", "application/pdf"

    async def _delete(s3_key):
        return None

    monkeypatch.setattr(router_mod.file_storage, "upload_file", _upload)
    monkeypatch.setattr(router_mod.file_storage, "get_file_stream", _stream)
    monkeypatch.setattr(router_mod.file_storage, "delete_file", _delete)


async def _upload(client, token, dog_id, kind="vet_passport", valid_until="2030-01-01"):
    data = {"kind": kind}
    if valid_until:
        data["valid_until"] = valid_until
    return await client.post(
        f"/dogs/{dog_id}/documents",
        data=data,
        files={"file": ("vet.pdf", b"%PDF-1.4", "application/pdf")},
        headers=auth(token),
    )


async def test_owner_uploads_and_lists(client, db_session):
    owner_id, token = await make_api_user(client)
    from app.models.user import User
    owner = await db_session.get(User, owner_id)
    w = await make_world(db_session, owner=owner)

    r = await _upload(client, token, w.dog.id)
    assert r.status_code == 201, r.text
    first = r.json()
    assert first["kind"] == "vet_passport" and first["is_current"] is True

    r = await _upload(client, token, w.dog.id, valid_until="2031-01-01")
    assert r.status_code == 201

    r = await client.get(f"/dogs/{w.dog.id}/documents", headers=auth(token))
    assert r.status_code == 200
    docs = r.json()
    assert len(docs) == 2
    current = [d for d in docs if d["is_current"]]
    assert len(current) == 1 and current[0]["valid_until"] == "2031-01-01"


async def test_stranger_cannot_upload_or_list(client, db_session):
    _, stranger = await make_api_user(client)
    w = await make_world(db_session)
    assert (await _upload(client, stranger, w.dog.id)).status_code == 403
    assert (await client.get(f"/dogs/{w.dog.id}/documents", headers=auth(stranger))).status_code == 403


async def test_download_acl(client, db_session):
    owner_id, owner_token = await make_api_user(client)
    reg_id, reg_token = await make_api_user(client)
    other_reg_id, other_reg_token = await make_api_user(client)
    from app.models.user import User
    owner = await db_session.get(User, owner_id)
    w = await make_world(db_session, owner=owner)
    other = await make_world(db_session)
    db_session.add(ShowStaff(show_id=w.show.id, user_id=reg_id))
    db_session.add(ShowStaff(show_id=other.show.id, user_id=other_reg_id))
    await db_session.commit()

    doc = (await _upload(client, owner_token, w.dog.id)).json()
    url = f"/dogs/{w.dog.id}/documents/{doc['id']}/download"

    assert (await client.get(url, headers=auth(owner_token))).status_code == 200
    r = await client.get(url, headers=auth(reg_token))
    assert r.status_code == 200 and r.content == b"%PDF"
    assert (await client.get(url, headers=auth(other_reg_token))).status_code == 404

    # Выставка завершена — регистратор больше не видит документы.
    w.show.status = ShowStatus.completed
    await db_session.commit()
    assert (await client.get(url, headers=auth(reg_token))).status_code == 404


async def test_delete_document(client, db_session):
    owner_id, token = await make_api_user(client)
    from app.models.user import User
    owner = await db_session.get(User, owner_id)
    w = await make_world(db_session, owner=owner)
    doc = (await _upload(client, token, w.dog.id)).json()
    r = await client.delete(f"/dogs/{w.dog.id}/documents/{doc['id']}", headers=auth(token))
    assert r.status_code == 204
    r = await client.get(f"/dogs/{w.dog.id}/documents", headers=auth(token))
    assert r.json() == []


async def test_document_of_other_dog_404(client, db_session):
    owner_id, token = await make_api_user(client)
    from app.models.user import User
    owner = await db_session.get(User, owner_id)
    w = await make_world(db_session, owner=owner)
    w2 = await make_world(db_session, owner=owner)
    doc = (await _upload(client, token, w.dog.id)).json()
    r = await client.get(f"/dogs/{w2.dog.id}/documents/{doc['id']}/download", headers=auth(token))
    assert r.status_code == 404
