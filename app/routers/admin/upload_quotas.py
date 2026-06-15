"""
Админ-CRUD лимитов квот загрузки файлов.

Тиры фиксированы (untrusted/standard/breeder) — поэтому только list +
update известных строк (create/delete не нужны). Под ролью admin на
уровне роутера, как admin/references и переключатель feature-flags.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import require_any_role
from app.repositories import upload_quota as repo
from app.schemas.upload_quota import UploadQuotaTierResponse, UploadQuotaUpdate
from app.services.upload_quota import UploadTier

router = APIRouter(
    prefix="/admin/upload-quotas",
    tags=["admin-upload-quotas"],
    dependencies=[Depends(require_any_role("admin"))],
)


@router.get("", response_model=list[UploadQuotaTierResponse])
async def list_upload_quotas(db: AsyncSession = Depends(get_db)):
    """Лимиты всех тиров."""
    return await repo.list_tier_configs(db)


@router.put("/{tier}", response_model=UploadQuotaTierResponse)
async def update_upload_quota(
    tier: str,
    body: UploadQuotaUpdate,
    db: AsyncSession = Depends(get_db),
):
    """
    Изменить лимиты тира. Неизвестный тир → 404 (нельзя писать вне
    фиксированного набора).
    """
    if tier not in {t.value for t in UploadTier}:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="unknown tier"
        )
    updated = await repo.update_tier_config(
        db, tier, body.daily_limit, body.max_storage_bytes
    )
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="tier not found"
        )
    await db.commit()
    return updated
