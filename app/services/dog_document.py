"""
Документы собаки (ветпаспорт, родословная, ...) для допуска на выставки.

Права:
- загрузка/удаление/список — тот, кто управляет собакой (владелец,
  владелец питомника, admin) — как у фото;
- просмотр/скачивание — ещё и организатор/персонал активной выставки,
  где у собаки есть запись (им нужно сверить документы на стойке).
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dog import Dog, DogDocument, DogDocumentKind
from app.models.file import UploadedFile
from app.repositories import checkin as checkin_repo
from app.repositories import dog as dog_repo
from app.schemas.checkin import DogDocumentResponse
from app.services import checkin_rules
from app.services.dog import _check_can_manage_dog


async def get_dog_or_404(db: AsyncSession, dog_id: uuid.UUID) -> Dog:
    dog = await dog_repo.get_dog(db, dog_id)
    if dog is None:
        raise ValueError("dog_not_found")
    return dog


async def ensure_can_manage(
    db: AsyncSession, dog: Dog, user_id: uuid.UUID, is_admin: bool
) -> None:
    await _check_can_manage_dog(db, dog, user_id, is_admin)


async def can_view(db: AsyncSession, dog: Dog, user_id: uuid.UUID, is_admin: bool) -> bool:
    try:
        await _check_can_manage_dog(db, dog, user_id, is_admin)
        return True
    except ValueError:
        return await checkin_repo.user_has_show_access_to_dog(db, dog.id, user_id)


def to_responses(rows: list[tuple[DogDocument, UploadedFile]]) -> list[DogDocumentResponse]:
    current_ids = {
        d.id for d in checkin_rules.current_documents([d for d, _ in rows]).values()
    }
    return [
        DogDocumentResponse(
            id=d.id, dog_id=d.dog_id, kind=d.kind, valid_until=d.valid_until,
            created_at=d.created_at, original_filename=f.original_filename,
            content_type=f.content_type, size_bytes=f.size_bytes,
            is_current=d.id in current_ids,
        )
        for d, f in rows
    ]


async def create_document(
    db: AsyncSession,
    *,
    dog: Dog,
    user_id: uuid.UUID,
    kind: DogDocumentKind,
    valid_until: date | None,
    s3_key: str,
    content_type: str,
    filename: str,
    size_bytes: int,
) -> DogDocumentResponse:
    f = UploadedFile(
        uploaded_by=user_id, s3_key=s3_key, original_filename=filename,
        content_type=content_type, size_bytes=size_bytes,
        is_public=False,  # ПДн + ветданные — только через ACL-эндпоинт
    )
    db.add(f)
    await db.flush()
    doc = DogDocument(
        dog_id=dog.id, file_id=f.id, kind=kind, valid_until=valid_until,
        uploaded_by=user_id,
    )
    db.add(doc)
    await db.commit()
    rows = await checkin_repo.list_dog_documents(db, [dog.id])
    return next(r for r in to_responses(rows) if r.id == doc.id)
