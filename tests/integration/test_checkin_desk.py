# tests/integration/test_checkin_desk.py
"""Интеграция: стойка регистрации — билет, скан, поиск, отметки, сводка."""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from app.models.dog import DogDocument, DogDocumentKind
from app.models.file import UploadedFile
from app.models.show import ShowStaff, ShowStatus
from app.models.user import User
from app.utils.checkin_token import make_token
from tests.integration.checkin_helpers import add_entry, auth, make_api_user, make_world

ADMIT = {"checks": [
    {"kind": "arrival", "result": "passed"},
    {"kind": "vet", "result": "passed"},
    {"kind": "docs_onsite", "result": "passed"},
]}


async def _setup(client, db_session, **kw):
    """Организатор (API), владелец (API), регистратор (API) + выставка."""
    org_id, org_t = await make_api_user(client)
    own_id, own_t = await make_api_user(client)
    reg_id, reg_t = await make_api_user(client)
    w = await make_world(
        db_session,
        organizer=await db_session.get(User, org_id),
        owner=await db_session.get(User, own_id),
        **kw,
    )
    db_session.add(ShowStaff(show_id=w.show.id, user_id=reg_id))
    await db_session.commit()
    return w, org_t, own_t, reg_t


async def _add_doc(db_session, w, kind, valid_until=None):
    f = UploadedFile(
        uploaded_by=w.owner.id, s3_key=f"dog-documents/{uuid.uuid4()}.pdf",
        original_filename="d.pdf", content_type="application/pdf", size_bytes=1,
        is_public=False,
    )
    db_session.add(f)
    await db_session.flush()
    d = DogDocument(dog_id=w.dog.id, file_id=f.id, kind=kind, valid_until=valid_until)
    db_session.add(d)
    await db_session.commit()
    return d


async def test_ticket_and_scan(client, db_session):
    w, org_t, own_t, reg_t = await _setup(client, db_session)
    await add_entry(db_session, w, owner=w.owner, name="Вторая")

    r = await client.get(f"/shows/{w.show.id}/my-ticket", headers=auth(own_t))
    assert r.status_code == 200, r.text
    ticket = r.json()
    assert ticket["token"].startswith("SR1.") and len(ticket["entries"]) == 2
    assert "vet_passport_missing" in ticket["entries"][0]["problems"]

    r = await client.post(
        f"/shows/{w.show.id}/checkin/scan", json={"token": ticket["token"]}, headers=auth(reg_t)
    )
    assert r.status_code == 200, r.text
    card = r.json()
    assert card["user_id"] == str(w.owner.id) and len(card["entries"]) == 2
    first = card["entries"][0]
    assert first["dog"]["microchip"] == w.dog.microchip
    assert first["attendance_status"] == "registered"


async def test_scan_errors(client, db_session):
    w, org_t, own_t, reg_t = await _setup(client, db_session)
    other = await make_world(db_session)
    r = await client.post(f"/shows/{w.show.id}/checkin/scan", json={"token": "garbage"}, headers=auth(reg_t))
    assert r.status_code == 400 and r.json()["detail"] == "invalid_token"
    r = await client.post(
        f"/shows/{w.show.id}/checkin/scan",
        json={"token": make_token(other.show.id, w.owner.id)}, headers=auth(reg_t),
    )
    assert r.status_code == 404 and r.json()["detail"] == "token_other_show"
    r = await client.post(
        f"/shows/{w.show.id}/checkin/scan",
        json={"token": make_token(w.show.id, uuid.uuid4())}, headers=auth(reg_t),
    )
    assert r.status_code == 404 and r.json()["detail"] == "no_entries"


async def test_foreign_registrar_forbidden(client, db_session):
    w, org_t, own_t, reg_t = await _setup(client, db_session)
    # Вторая выставка — напрямую в БД: лимит /auth/register — 3 на IP в час.
    other = await make_world(db_session)
    # reg_t — регистратор выставки w, но не other.
    token = make_token(other.show.id, other.owner.id)
    r = await client.post(f"/shows/{other.show.id}/checkin/scan", json={"token": token}, headers=auth(reg_t))
    assert r.status_code == 403
    r = await client.post(f"/shows/{other.show.id}/entries/{other.entry.id}/checks", json=ADMIT, headers=auth(reg_t))
    assert r.status_code == 403
    # Запись чужой выставки через «свою» выставку — 404.
    r = await client.post(f"/shows/{w.show.id}/entries/{other.entry.id}/checks", json=ADMIT, headers=auth(reg_t))
    assert r.status_code == 404


async def test_admit_in_one_request_and_history(client, db_session):
    w, org_t, own_t, reg_t = await _setup(client, db_session)
    r = await client.post(f"/shows/{w.show.id}/entries/{w.entry.id}/checks", json=ADMIT, headers=auth(reg_t))
    assert r.status_code == 200, r.text
    assert r.json()["attendance_status"] == "admitted"
    r = await client.get(f"/shows/{w.show.id}/entries/{w.entry.id}/checks", headers=auth(reg_t))
    assert len(r.json()) == 3


async def test_correction_flips_rejected_to_admitted(client, db_session):
    w, org_t, own_t, reg_t = await _setup(client, db_session)
    reject = {"checks": [
        {"kind": "arrival", "result": "passed"},
        {"kind": "vet", "result": "failed", "comment": "нет прививки"},
    ]}
    r = await client.post(f"/shows/{w.show.id}/entries/{w.entry.id}/checks", json=reject, headers=auth(reg_t))
    assert r.json()["attendance_status"] == "rejected"
    r = await client.post(f"/shows/{w.show.id}/entries/{w.entry.id}/checks", json=ADMIT, headers=auth(reg_t))
    assert r.json()["attendance_status"] == "admitted"
    r = await client.get(f"/shows/{w.show.id}/entries/{w.entry.id}/checks", headers=auth(reg_t))
    assert len(r.json()) == 5


async def test_failed_without_comment_and_atomicity(client, db_session):
    w, org_t, own_t, reg_t = await _setup(client, db_session)
    other = await make_world(db_session)
    doc = await _add_doc(db_session, other, DogDocumentKind.vet_passport)
    r = await client.post(
        f"/shows/{w.show.id}/entries/{w.entry.id}/checks",
        json={"checks": [{"kind": "vet", "result": "failed"}]}, headers=auth(reg_t),
    )
    assert r.status_code == 422
    # Вторая отметка ссылается на документ чужой собаки → ничего не записано.
    r = await client.post(
        f"/shows/{w.show.id}/entries/{w.entry.id}/checks",
        json={"checks": [
            {"kind": "arrival", "result": "passed"},
            {"kind": "vet", "result": "passed", "document_id": str(doc.id)},
        ]},
        headers=auth(reg_t),
    )
    assert r.status_code == 422
    r = await client.get(f"/shows/{w.show.id}/entries/{w.entry.id}/checks", headers=auth(reg_t))
    assert r.json() == []


async def test_status_windows(client, db_session):
    w, org_t, own_t, reg_t = await _setup(client, db_session, status=ShowStatus.registration_open)
    r = await client.post(f"/shows/{w.show.id}/entries/{w.entry.id}/checks", json=ADMIT, headers=auth(reg_t))
    assert r.status_code == 409 and r.json()["detail"] == "invalid_show_status"
    pre = {"checks": [{"kind": "docs_precheck", "result": "passed"}]}
    r = await client.post(f"/shows/{w.show.id}/entries/{w.entry.id}/checks", json=pre, headers=auth(org_t))
    assert r.status_code == 200
    w.show.checkin_enabled = False
    await db_session.commit()
    r = await client.post(f"/shows/{w.show.id}/entries/{w.entry.id}/checks", json=pre, headers=auth(org_t))
    assert r.status_code == 409 and r.json()["detail"] == "checkin_disabled"


async def test_scan_after_checks_shows_latest_state(client, db_session):
    w, org_t, own_t, reg_t = await _setup(client, db_session)
    await _add_doc(db_session, w, DogDocumentKind.vet_passport, date.today() + timedelta(days=30))
    await client.post(f"/shows/{w.show.id}/entries/{w.entry.id}/checks", json=ADMIT, headers=auth(reg_t))
    token = (await client.get(f"/shows/{w.show.id}/my-ticket", headers=auth(own_t))).json()["token"]
    card = (await client.post(f"/shows/{w.show.id}/checkin/scan", json={"token": token}, headers=auth(reg_t))).json()
    e = card["entries"][0]
    assert e["attendance_status"] == "admitted"
    assert e["latest_checks"]["vet"]["result"] == "passed"
    assert e["rabies_valid_for_show"] is True
    assert len([d for d in e["documents"] if d["kind"] == "vet_passport"]) == 1


async def test_search_and_summary(client, db_session):
    w, org_t, own_t, reg_t = await _setup(client, db_session)
    for q in (str(w.entry.catalog_number), w.dog.microchip, w.dog.name[:4]):
        r = await client.get(f"/shows/{w.show.id}/checkin/search", params={"q": q}, headers=auth(reg_t))
        assert r.status_code == 200, (q, r.text)
        assert [c["entry_id"] for c in r.json()] == [str(w.entry.id)], q
    await client.post(f"/shows/{w.show.id}/entries/{w.entry.id}/checks", json=ADMIT, headers=auth(reg_t))
    r = await client.get(f"/shows/{w.show.id}/checkin/summary", headers=auth(reg_t))
    assert r.json() == {"total": 1, "registered": 0, "arrived": 0, "admitted": 1, "rejected": 0, "absent": 0}


async def test_precheck_queue(client, db_session):
    w, org_t, own_t, reg_t = await _setup(client, db_session, status=ShowStatus.registration_open)
    r = await client.get(f"/shows/{w.show.id}/checkin/precheck-queue", headers=auth(org_t))
    assert r.json() == []  # без документов в очереди нечего смотреть
    await _add_doc(db_session, w, DogDocumentKind.pedigree)
    r = await client.get(f"/shows/{w.show.id}/checkin/precheck-queue", headers=auth(org_t))
    assert [c["entry_id"] for c in r.json()] == [str(w.entry.id)]
    await client.post(
        f"/shows/{w.show.id}/entries/{w.entry.id}/checks",
        json={"checks": [{"kind": "docs_precheck", "result": "passed"}]}, headers=auth(org_t),
    )
    r = await client.get(f"/shows/{w.show.id}/checkin/precheck-queue", headers=auth(org_t))
    assert r.json() == []
