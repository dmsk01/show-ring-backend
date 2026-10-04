"""
Документы собаки (чек-ин выставок): загрузка, список, скачивание, удаление.

Файлы приватные (files.is_public=False): публичный GET /files/{id} их
не отдаёт, скачивание — только здесь, после ACL.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date
from typing import NoReturn
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.dependencies import get_current_user, is_admin
from app.models.dog import DogDocumentKind
from app.models.user import User
from app.repositories import checkin as checkin_repo
from app.schemas.checkin import DogDocumentResponse
from app.services import dog_document as svc
from app.services import file_storage, upload_quota

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dogs", tags=["dog-documents"])


def _raise_for_error(err: ValueError) -> NoReturn:
    code = str(err)
    if code.endswith("not_found"):
        raise HTTPException(404, code)
    if code == "forbidden":
        raise HTTPException(403, code)
    raise HTTPException(400, code)


@router.post(
    "/{dog_id}/documents",
    response_model=DogDocumentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Загрузить документ собаки",
)
async def upload_document(
    dog_id: uuid.UUID,
    file: UploadFile = File(...),
    kind: DogDocumentKind = Form(...),
    valid_until: date | None = Form(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        dog = await svc.get_dog_or_404(db, dog_id)
        await svc.ensure_can_manage(db, dog, user.id, is_admin(user))
    except ValueError as e:
        _raise_for_error(e)
    try:
        await upload_quota.check_upload_quota(
            db, user, declared_size_bytes=file.size or settings.max_upload_size_bytes
        )
    except upload_quota.UploadQuotaExceeded as e:
        return JSONResponse(status_code=e.status_code, content=e.body, headers=e.headers)
    s3_key, ct, filename, size_bytes = await file_storage.upload_file(
        file, folder="dog-documents"
    )
    return await svc.create_document(
        db, dog=dog, user_id=user.id, kind=kind, valid_until=valid_until,
        s3_key=s3_key, content_type=ct, filename=filename, size_bytes=size_bytes,
    )


@router.get(
    "/{dog_id}/documents",
    response_model=list[DogDocumentResponse],
    summary="Документы собаки",
)
async def list_documents(
    dog_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        dog = await svc.get_dog_or_404(db, dog_id)
    except ValueError as e:
        _raise_for_error(e)
    if not await svc.can_view(db, dog, user.id, is_admin(user)):
        raise HTTPException(403, "forbidden")
    return svc.to_responses(await checkin_repo.list_dog_documents(db, [dog.id]))


@router.delete(
    "/{dog_id}/documents/{doc_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Удалить документ собаки",
)
async def delete_document(
    dog_id: uuid.UUID,
    doc_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        dog = await svc.get_dog_or_404(db, dog_id)
        await svc.ensure_can_manage(db, dog, user.id, is_admin(user))
    except ValueError as e:
        _raise_for_error(e)
    row = await checkin_repo.get_dog_document(db, dog_id, doc_id)
    if row is None:
        raise HTTPException(404, "document_not_found")
    _doc, f = row
    s3_key = f.s3_key
    # Удаляем запись files — dog_documents уйдёт каскадом,
    # entry_checks.document_id → NULL (история отметок сохраняется).
    await db.delete(f)
    await db.commit()
    try:
        await file_storage.delete_file(s3_key)
    except Exception:
        logger.warning("Failed to delete document blob %s", s3_key, exc_info=True)


@router.get(
    "/{dog_id}/documents/{doc_id}/download",
    summary="Скачать документ собаки",
    response_class=Response,
)
async def download_document(
    dog_id: uuid.UUID,
    doc_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        dog = await svc.get_dog_or_404(db, dog_id)
    except ValueError as e:
        _raise_for_error(e)
    # 404, а не 403: не раскрываем существование документа постороннему.
    if not await svc.can_view(db, dog, user.id, is_admin(user)):
        raise HTTPException(404, "document_not_found")
    row = await checkin_repo.get_dog_document(db, dog_id, doc_id)
    if row is None:
        raise HTTPException(404, "document_not_found")
    _doc, f = row
    body, content_type = await file_storage.get_file_stream(f.s3_key)
    safe_name = quote(f.original_filename or "document", safe="")
    return Response(
        content=body,
        media_type=content_type,
        headers={
            "Content-Disposition": f"inline; filename*=UTF-8''{safe_name}",
            "Cache-Control": "private, no-store",
        },
    )
