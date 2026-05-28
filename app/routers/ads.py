"""
Роутер рекламного модуля (этап 10).

Эндпоинты:
- /ads/campaigns       — CRUD кампаний для рекламодателя
- /ads/campaigns/{id}/banners — добавление баннеров
- /ads/banners/{id}    — обновление баннера
- /ads/serve           — public: получить баннер по контексту
- /ads/events          — public: зафиксировать impression/click
- /ads/campaigns/{id}/stats        — total статистика
- /ads/campaigns/{id}/stats/daily  — дневная разбивка
"""

from __future__ import annotations

import uuid
from typing import NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.middleware.progressive_ban import check_rate_limit
from app.models.user import User
from app.redis import get_redis
from app.repositories import ad as repo
from app.schemas.ad import (
    AdEventCreate,
    BannerCreate,
    BannerResponse,
    BannerUpdate,
    CampaignCreate,
    CampaignResponse,
    CampaignStats,
    CampaignUpdate,
    DailyStat,
    ServeResponse,
)
from app.services import ad as svc

router = APIRouter(prefix="/ads", tags=["ads"])


def _is_admin(user: User) -> bool:
    return any(r.role.value == "admin" for r in user.roles)


def _raise_for_error(err: ValueError) -> NoReturn:
    code = str(err)
    not_found = {"not_found", "campaign_not_found", "banner_not_found"}
    if code in not_found:
        raise HTTPException(404, code)
    if code == "forbidden":
        raise HTTPException(403, code)
    if code == "banner_inactive":
        raise HTTPException(409, code)
    raise HTTPException(400, code)


# ---------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------


@router.post(
    "/campaigns",
    response_model=CampaignResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Создать рекламную кампанию",
)
async def create_campaign(
    body: CampaignCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    obj = await svc.create_campaign(
        db, advertiser_id=user.id, fields=body.model_dump()
    )
    return CampaignResponse.model_validate(obj)


@router.get(
    "/campaigns",
    response_model=list[CampaignResponse],
    summary="Мои кампании",
)
async def list_my_campaigns(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    items = await repo.list_campaigns_for_advertiser(db, user.id)
    return [CampaignResponse.model_validate(c) for c in items]


@router.put(
    "/campaigns/{campaign_id}",
    response_model=CampaignResponse,
    summary="Обновить кампанию",
)
async def update_campaign(
    campaign_id: uuid.UUID,
    body: CampaignUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        obj = await svc.update_campaign(
            db,
            campaign_id=campaign_id,
            user_id=user.id,
            is_admin=_is_admin(user),
            fields=body.model_dump(exclude_unset=True),
        )
    except ValueError as e:
        _raise_for_error(e)
    return CampaignResponse.model_validate(obj)


# ---------------------------------------------------------------------
# Banners
# ---------------------------------------------------------------------


@router.post(
    "/campaigns/{campaign_id}/banners",
    response_model=BannerResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Добавить баннер в кампанию",
)
async def create_banner(
    campaign_id: uuid.UUID,
    body: BannerCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        obj = await svc.create_banner(
            db,
            campaign_id=campaign_id,
            user_id=user.id,
            is_admin=_is_admin(user),
            fields=body.model_dump(),
        )
    except ValueError as e:
        _raise_for_error(e)
    return BannerResponse.model_validate(obj)


@router.put(
    "/banners/{banner_id}",
    response_model=BannerResponse,
    summary="Обновить баннер",
)
async def update_banner(
    banner_id: uuid.UUID,
    body: BannerUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        obj = await svc.update_banner(
            db,
            banner_id=banner_id,
            user_id=user.id,
            is_admin=_is_admin(user),
            fields=body.model_dump(exclude_unset=True),
        )
    except ValueError as e:
        _raise_for_error(e)
    return BannerResponse.model_validate(obj)


# ---------------------------------------------------------------------
# Public: serve и events
# ---------------------------------------------------------------------


@router.get(
    "/serve",
    response_model=ServeResponse | None,
    summary="Получить баннер для показа",
    description=(
        "Public endpoint. По контексту страницы (placement + опциональные "
        "animal_type/breed/region) возвращает один подходящий баннер. "
        "Если ни один не подошёл — null."
    ),
)
async def serve_banner(
    placement: str = Query(..., max_length=64),
    animal_type_id: uuid.UUID | None = Query(None),
    breed_id: uuid.UUID | None = Query(None),
    region: str | None = Query(None, max_length=128),
    db: AsyncSession = Depends(get_db),
):
    banner = await svc.pick_banner(
        db,
        placement=placement,
        animal_type_id=animal_type_id,
        breed_id=breed_id,
        region=region,
    )
    if banner is None:
        return None
    return ServeResponse(
        banner_id=banner.id,
        image_file_id=banner.image_file_id,
        target_url=banner.target_url,
        title=banner.title,
        placement=banner.placement,
    )


@router.post(
    "/events",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Зафиксировать impression/click",
    description=(
        "Public endpoint, без авторизации (анонимы тоже видят рекламу). "
        "Дедупликация по (banner + ip + user_agent + тип) с TTL=60s "
        "через Redis. ip и user_agent сервер берёт из заголовков, "
        "клиент не может их подделать."
    ),
)
async def record_event(
    body: AdEventCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    # bug_213 audit 2026-05-28: public-эндпоинт без авторизации —
    # обязателен rate-limit поверх дедупа. Дедуп ловит «одинаковые»
    # повторы (banner+ip+ua+type) на 60s, но атакующий с ротацией
    # User-Agent пробивает дедуп и накручивает разные ключи. Лимит на
    # IP режет ботнет до устойчивого фона, не мешая обычным просмотрам.
    await check_rate_limit(
        request,
        limit=120,
        window=60,
        redis=redis,
    )
    # ip и user_agent — из заголовков HTTP. Клиент не может подделать
    # (для X-Forwarded-For нужно доверять только реверс-прокси, что мы
    # настроим на этапе 15 при выкатке за nginx).
    ip = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")
    # user_id если есть в JWT — фиксируем для аналитики. Авторизация
    # не требуется, но если токен прислали — используем.
    user_id: uuid.UUID | None = None
    # current_user не дёргаем (это вызовет 401 у анонимов).
    # Если на этапе 11 нужно будет привязывать user_id к событиям —
    # сделаем optional_current_user dependency.

    try:
        accepted = await svc.record_event(
            db,
            banner_id=body.banner_id,
            event_type=body.event_type,
            user_id=user_id,
            ip=ip,
            user_agent=user_agent,
            page_url=body.page_url,
        )
    except ValueError as e:
        _raise_for_error(e)
    return {"accepted": accepted}


# ---------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------


@router.get(
    "/campaigns/{campaign_id}/stats",
    response_model=CampaignStats,
    summary="Сводная статистика по кампании",
)
async def campaign_stats(
    campaign_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        data = await svc.stats_for_campaign(
            db,
            campaign_id=campaign_id,
            user_id=user.id,
            is_admin=_is_admin(user),
        )
    except ValueError as e:
        _raise_for_error(e)
    return CampaignStats(**data)


@router.get(
    "/campaigns/{campaign_id}/stats/daily",
    response_model=list[DailyStat],
    summary="Дневная статистика по кампании",
)
async def campaign_daily_stats(
    campaign_id: uuid.UUID,
    days: int = Query(30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        rows = await svc.daily_stats(
            db,
            campaign_id=campaign_id,
            user_id=user.id,
            is_admin=_is_admin(user),
            days=days,
        )
    except ValueError as e:
        _raise_for_error(e)
    return [DailyStat(**r) for r in rows]
