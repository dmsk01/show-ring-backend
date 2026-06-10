"""
Схемы админ-эндпоинтов (этап 12).

Все ответы аналитики строятся "на лету" из raw SQL — поэтому
Pydantic-модели сделаны фигурой dict-to-schema без from_attributes.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from app.models.classified import ClassifiedStatus
from app.models.user import RoleEnum


# ---------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------


class DashboardStats(BaseModel):
    """
    Сводка платформы. Все поля int, потому что COUNT(*) в PG возвращает
    bigint, но pydantic int с лёгкостью съедает.
    """

    total_users: int
    verified_kennels: int
    total_kennels: int
    total_dogs: int
    total_breeds: int
    completed_shows: int
    open_shows: int
    active_classifieds: int
    total_litters: int
    active_campaigns: int
    # bug_226 audit 2026-05-28: момент пересчёта значений. Клиент видит,
    # что цифры могут отставать до 5 минут (TTL кеша). При cache miss
    # last_updated_at ≈ «сейчас», при hit — момент предыдущего SELECT'а.
    last_updated_at: datetime


# ---------------------------------------------------------------------
# Топы и отчёты
# ---------------------------------------------------------------------


class TopBreedRow(BaseModel):
    breed_id: uuid.UUID
    breed_name: str
    entries_count: int


class ShowReportRow(BaseModel):
    breed_name: str
    class_name: str
    # class_age_from нужен только для сортировки на бэкенде; отдаём
    # фронту тоже — для возможной вторичной фильтрации.
    class_age_from: int
    entries: int
    cw_count: int
    bob_count: int
    first_place_count: int


class ShowRevenueEstimate(BaseModel):
    entry_fee: Decimal | None
    entries_count: int
    revenue_estimate: Decimal


class AdsDailyRow(BaseModel):
    day: date
    impressions: int
    clicks: int
    # ctr_percent может быть None если за день не было impressions
    # (PG NULLIF возвращает NULL, ROUND его пропускает).
    ctr_percent: Decimal | None


class TopCampaignRow(BaseModel):
    id: uuid.UUID
    name: str
    spent: Decimal
    budget: Decimal
    spent_percent: Decimal | None


# ---------------------------------------------------------------------
# Модерация
# ---------------------------------------------------------------------


class ClassifiedModerationDecision(BaseModel):
    """
    Решение модератора по объявлению.

    approve=True переводит статус active. approve=False → closed
    с причиной. Причина обязательна при отказе для прозрачности
    (видна автору в /notifications, когда будет добавлен соответствующий
    EventType).
    """

    approve: bool
    reason: str | None = Field(None, max_length=500)


class KennelVerifyRequest(BaseModel):
    is_verified: bool


class UserRoleUpdateRequest(BaseModel):
    role: RoleEnum
    # action=grant — добавить роль; revoke — удалить.
    grant: bool = True


class UserBlockRequest(BaseModel):
    is_active: bool


# ---------------------------------------------------------------------
# Списки на модерацию
# ---------------------------------------------------------------------


class ClassifiedModerationItem(BaseModel):
    """Краткая карточка объявления для модерации."""

    id: uuid.UUID
    author_id: uuid.UUID
    title: str
    category: str
    status: ClassifiedStatus
    created_at: date


class KennelModerationItem(BaseModel):
    id: uuid.UUID
    owner_id: uuid.UUID
    name: str
    kennel_prefix: str | None
    is_verified: bool


class UserAdminItem(BaseModel):
    id: uuid.UUID
    email: str | None  # None у пользователей, вошедших по телефону (phone-OTP)
    is_active: bool
    is_email_verified: bool
    roles: list[RoleEnum]
