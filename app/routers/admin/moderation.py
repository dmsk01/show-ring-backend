"""
Модерация и управление пользователями (этап 12).

Все эндпоинты защищены ролью admin. Список объявлений/питомников
на модерации — простые SELECT'ы с фильтрами; решения — через сервис.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user, require_any_role  # noqa: F401
from app.models.ad import AdCampaign, CampaignStatus
from app.models.classified import Classified, ClassifiedStatus
from app.models.kennel import Kennel
from app.models.user import User, UserRole
from app.schemas.ad import CampaignResponse
from app.schemas.admin import (
    CampaignModerationDecision,
    CampaignModerationItem,
    ClassifiedModerationDecision,
    ClassifiedModerationItem,
    KennelModerationItem,
    KennelVerifyRequest,
    UserAdminItem,
    UserBlockRequest,
    UserRoleUpdateRequest,
)
from app.services import moderation as svc

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_any_role("admin"))],
)


# ---------------------------------------------------------------------
# Classifieds
# ---------------------------------------------------------------------


@router.get(
    "/moderation/classifieds",
    response_model=list[ClassifiedModerationItem],
    summary="Объявления на модерации",
)
async def list_classifieds_on_moderation(
    status_: ClassifiedStatus = Query(
        ClassifiedStatus.moderation, alias="status"
    ),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(Classified)
        .where(Classified.status == status_)
        .order_by(Classified.created_at.asc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    items = (await db.execute(stmt)).scalars().all()
    return [
        ClassifiedModerationItem(
            id=c.id,
            author_id=c.author_id,
            title=c.title,
            category=c.category.value,
            status=c.status,
            created_at=c.created_at.date(),
        )
        for c in items
    ]


@router.put(
    "/moderation/classifieds/{classified_id}",
    summary="Одобрить или отклонить объявление",
)
async def moderate_classified(
    classified_id: uuid.UUID,
    body: ClassifiedModerationDecision,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    obj = await svc.moderate_classified(
        db,
        classified_id,
        body.approve,
        body.reason,
        actor_id=actor.id,
    )
    return {"id": obj.id, "status": obj.status.value}


# ---------------------------------------------------------------------
# Campaigns (ревью фронтенда 2026-10-09, FE-37)
# ---------------------------------------------------------------------


@router.get(
    "/moderation/campaigns",
    response_model=list[CampaignModerationItem],
    summary="Рекламные кампании на модерации",
    description=(
        "Кампании, которые ещё ни разу не одобрял админ (approved_at пуст) и "
        "которые не завершены и не отменены. Старые — первыми."
    ),
)
async def list_campaigns_on_moderation(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(AdCampaign, User.email)
        .join(User, User.id == AdCampaign.advertiser_id)
        .where(
            AdCampaign.approved_at.is_(None),
            AdCampaign.status.notin_(
                [CampaignStatus.completed, CampaignStatus.cancelled]
            ),
        )
        .order_by(AdCampaign.created_at.asc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    rows = (await db.execute(stmt)).all()
    return [
        CampaignModerationItem(
            id=c.id,
            advertiser_id=c.advertiser_id,
            advertiser_email=email,
            advertiser_name=c.advertiser_name,
            name=c.name,
            budget=c.budget,
            cost_per_impression=c.cost_per_impression,
            date_start=c.date_start,
            date_end=c.date_end,
            status=c.status,
            created_at=c.created_at,
        )
        for c, email in rows
    ]


@router.put(
    "/moderation/campaigns/{campaign_id}",
    response_model=CampaignResponse,
    summary="Одобрить или отклонить рекламную кампанию",
)
async def moderate_campaign(
    campaign_id: uuid.UUID,
    body: CampaignModerationDecision,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    obj = await svc.moderate_campaign(
        db, campaign_id, body.approve, body.reason, actor_id=actor.id
    )
    return CampaignResponse.model_validate(obj)


# ---------------------------------------------------------------------
# Kennels
# ---------------------------------------------------------------------


@router.get(
    "/moderation/kennels",
    response_model=list[KennelModerationItem],
    summary="Питомники (для верификации)",
)
async def list_kennels_for_moderation(
    only_unverified: bool = Query(True),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Kennel)
    if only_unverified:
        stmt = stmt.where(Kennel.is_verified.is_(False))
    stmt = (
        stmt.order_by(Kennel.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    items = (await db.execute(stmt)).scalars().all()
    return [
        KennelModerationItem(
            id=k.id,
            owner_id=k.owner_id,
            name=k.name,
            kennel_prefix=k.kennel_prefix,
            is_verified=k.is_verified,
        )
        for k in items
    ]


@router.put(
    "/moderation/kennels/{kennel_id}/verify",
    summary="Установить статус верификации питомника",
)
async def verify_kennel(
    kennel_id: uuid.UUID,
    body: KennelVerifyRequest,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    obj = await svc.verify_kennel(
        db, kennel_id, body.is_verified, actor_id=actor.id
    )
    return {"id": obj.id, "is_verified": obj.is_verified}


# ---------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------


@router.get(
    "/users",
    response_model=list[UserAdminItem],
    summary="Список пользователей",
)
async def list_users(
    is_active: bool | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(User)
    if is_active is not None:
        stmt = stmt.where(User.is_active.is_(is_active))
    stmt = stmt.order_by(User.created_at.desc()).offset(
        (page - 1) * per_page
    ).limit(per_page)
    users = (await db.execute(stmt)).scalars().all()

    # Подгружаем роли одним запросом: SELECT user_id, role FROM
    # user_roles WHERE user_id IN (...). Без этого получили бы N+1.
    user_ids = [u.id for u in users]
    if user_ids:
        roles_stmt = select(UserRole.user_id, UserRole.role).where(
            UserRole.user_id.in_(user_ids)
        )
        rows = (await db.execute(roles_stmt)).all()
        roles_by_user: dict[uuid.UUID, list] = {}
        for uid, role in rows:
            roles_by_user.setdefault(uid, []).append(role)
    else:
        roles_by_user = {}

    return [
        UserAdminItem(
            id=u.id,
            email=u.email,
            is_active=u.is_active,
            is_email_verified=u.is_email_verified,
            roles=roles_by_user.get(u.id, []),
        )
        for u in users
    ]


@router.put(
    "/users/{user_id}/block",
    summary="Заблокировать/разблокировать пользователя",
)
async def block_user(
    user_id: uuid.UUID,
    body: UserBlockRequest,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    obj = await svc.block_user(
        db, user_id, body.is_active, actor_id=actor.id
    )
    return {"id": obj.id, "is_active": obj.is_active}


@router.put(
    "/users/{user_id}/role",
    summary="Выдать или отозвать роль",
)
async def update_user_role(
    user_id: uuid.UUID,
    body: UserRoleUpdateRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_user),
):
    roles = await svc.update_user_role(
        db, user_id, body.role, body.grant, granted_by=admin.id
    )
    return {"id": str(user_id), "roles": [r.value for r in roles]}
