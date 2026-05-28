"""
Аналитические эндпоинты админки (этап 12).

Все эндпоинты защищены ролью admin. Аналитика по выставке доступна
также организатору (отдельный depends).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user, require_any_role
from app.models.show import Show
from app.models.user import User
from app.repositories import analytics as repo
from app.schemas.admin import (
    AdsDailyRow,
    DashboardStats,
    ShowReportRow,
    ShowRevenueEstimate,
    TopBreedRow,
    TopCampaignRow,
)

router = APIRouter(
    prefix="/admin/analytics",
    tags=["admin"],
    # require_any_role("admin") применяется ко всем эндпоинтам router'а
    # сразу: dependency на уровне маршрута короче, чем на каждом GET.
    dependencies=[Depends(require_any_role("admin"))],
)


def _period_start(days: int) -> datetime:
    """Удобный билдер: начало периода = сейчас минус N дней."""
    return datetime.now(timezone.utc) - timedelta(days=days)


@router.get(
    "/dashboard",
    response_model=DashboardStats,
    summary="Сводка платформы",
)
async def dashboard(db: AsyncSession = Depends(get_db)):
    return DashboardStats(**(await repo.dashboard(db)))


@router.get(
    "/top-breeds",
    response_model=list[TopBreedRow],
    summary="Топ пород по записям на выставки",
)
async def top_breeds(
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    rows = await repo.top_breeds_by_entries(
        db, _period_start(days), limit=limit
    )
    return [TopBreedRow(**r) for r in rows]


@router.get(
    "/ads",
    response_model=list[AdsDailyRow],
    summary="Рекламная аналитика по дням",
)
async def ads_analytics(
    days: int = Query(30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
):
    rows = await repo.ads_daily(db, _period_start(days))
    return [AdsDailyRow(**r) for r in rows]


@router.get(
    "/top-campaigns",
    response_model=list[TopCampaignRow],
    summary="Топ кампаний по потраченному бюджету",
)
async def top_campaigns(
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(10, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    rows = await repo.top_campaigns(db, _period_start(days), limit=limit)
    return [TopCampaignRow(**r) for r in rows]


# ---------------------------------------------------------------------
# Отчёт по выставке (admin или organizer-владелец)
# ---------------------------------------------------------------------


# Отчёт по выставке не наследует admin-only dependency, потому что
# организатор тоже имеет право видеть свой отчёт. Поэтому отдельный
# роутер без общего require_any_role.
show_report_router = APIRouter(
    prefix="/admin/analytics",
    tags=["admin"],
)


@show_report_router.get(
    "/shows/{show_id}/report",
    response_model=list[ShowReportRow],
    summary="Отчёт по выставке (admin или организатор)",
)
async def show_report(
    show_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # admin или организатор выставки. Проверка делается тут, а не
    # через dependency, потому что нужно знать сам show_id.
    show = await db.get(Show, show_id)
    if show is None:
        raise HTTPException(404, "not_found")
    is_admin = any(r.role.value == "admin" for r in user.roles)
    if not is_admin and show.organizer_id != user.id:
        raise HTTPException(403, "forbidden")
    rows = await repo.show_report(db, show_id)
    return [ShowReportRow(**r) for r in rows]


@show_report_router.get(
    "/shows/{show_id}/revenue",
    response_model=ShowRevenueEstimate,
    summary="Оценочная выручка выставки",
)
async def show_revenue(
    show_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    show = await db.get(Show, show_id)
    if show is None:
        raise HTTPException(404, "not_found")
    is_admin = any(r.role.value == "admin" for r in user.roles)
    if not is_admin and show.organizer_id != user.id:
        raise HTTPException(403, "forbidden")
    data = await repo.show_revenue_estimate(db, show_id)
    if data is None:
        raise HTTPException(404, "not_found")
    return ShowRevenueEstimate(**data)
