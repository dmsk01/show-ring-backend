# tests/integration/test_checkin_models.py
"""Интеграция: новые таблицы чек-ина создаются миграцией и пишутся ORM."""

from __future__ import annotations

import uuid
from datetime import date

from app.models.dog import DogDocument, DogDocumentKind
from app.models.file import UploadedFile
from app.models.show import (
    AttendanceStatus,
    EntryCheck,
    EntryCheckKind,
    EntryCheckResult,
    ShowStaff,
    ShowStaffRole,
)
from tests.integration.checkin_helpers import make_world


async def test_checkin_tables_roundtrip(db_session):
    w = await make_world(db_session)
    assert w.show.checkin_enabled is True
    assert w.entry.attendance_status == AttendanceStatus.registered

    f = UploadedFile(
        uploaded_by=w.owner.id, s3_key=f"dog-documents/{uuid.uuid4()}.pdf",
        original_filename="vet.pdf", content_type="application/pdf",
        size_bytes=10, is_public=False,
    )
    db_session.add(f)
    await db_session.flush()
    doc = DogDocument(
        dog_id=w.dog.id, file_id=f.id, kind=DogDocumentKind.vet_passport,
        valid_until=date(2030, 1, 1), uploaded_by=w.owner.id,
    )
    staff = ShowStaff(
        show_id=w.show.id, user_id=w.owner.id, role=ShowStaffRole.registrar,
        added_by=w.organizer.id,
    )
    db_session.add_all([doc, staff])
    await db_session.flush()
    check = EntryCheck(
        entry_id=w.entry.id, kind=EntryCheckKind.vet,
        result=EntryCheckResult.passed, document_id=doc.id,
        performed_by=w.organizer.id,
    )
    db_session.add(check)
    await db_session.commit()
    assert check.created_at is not None
    assert doc.created_at is not None
