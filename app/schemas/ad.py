"""
Схемы рекламного модуля (этап 10).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.ad import AdEventType, BannerPlacement, CampaignStatus


def _validate_target_url(v: str) -> str:
    """
    bug_211 audit 2026-05-28: regex `^https?://.+` пропускал ссылки
    вида `https://evil.com/redirect?to=javascript:alert(...)`. Сам по
    себе server-side не опасен, но если frontend рендерит target_url
    в `<a href="…">` и обработчик клика делает `window.location =
    this.href`, можно получить XSS. Делаем backend fail-closed:
    парсим URL и принимаем ТОЛЬКО https:// + непустой hostname.
    Заодно глушим SSRF-вектор (file://, gopher://) на случай, если
    target_url когда-то начнёт фетчиться сервером.
    """
    parsed = urlparse(v)
    if parsed.scheme != "https":
        raise ValueError("target_url must be https://")
    if not parsed.hostname:
        raise ValueError("target_url must have a hostname")
    # Hostname без точки = не публичный домен (localhost, internal).
    # Запрещаем — для рекламы интрасеть и так бессмысленна, а ssrf-
    # вектор закрыт.
    if "." not in parsed.hostname:
        raise ValueError("target_url hostname must contain a dot")
    return v


# ---------------------------------------------------------------------
# Campaign
# ---------------------------------------------------------------------


class CampaignBase(BaseModel):
    name: str = Field(..., max_length=255)
    description: str | None = None
    budget: Decimal = Field(..., gt=0)
    cost_per_impression: Decimal = Field(Decimal("0.01"), ge=0)
    date_start: date
    date_end: date

    @model_validator(mode="after")
    def _validate_dates(self) -> "CampaignBase":
        # date_end строго после date_start — кампания без длительности
        # бессмысленна, и валидаторы запросов /ads/serve проще пишутся
        # при гарантии "start < end".
        if self.date_end < self.date_start:
            raise ValueError("date_end must be >= date_start")
        return self


class CampaignCreate(CampaignBase):
    pass


class CampaignUpdate(BaseModel):
    name: str | None = Field(None, max_length=255)
    description: str | None = None
    budget: Decimal | None = Field(None, gt=0)
    cost_per_impression: Decimal | None = Field(None, ge=0)
    date_start: date | None = None
    date_end: date | None = None
    status: CampaignStatus | None = None


class CampaignResponse(CampaignBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    advertiser_id: uuid.UUID
    spent: Decimal
    status: CampaignStatus
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------


class BannerBase(BaseModel):
    image_file_id: uuid.UUID | None = None
    # target_url — только https://; парсер в _validate_target_url
    # отсекает javascript:, file://, http:// и URL без hostname.
    target_url: str = Field(..., max_length=2048)
    title: str | None = Field(None, max_length=255)
    placement: BannerPlacement
    target_animal_type_id: uuid.UUID | None = None
    target_breed_id: uuid.UUID | None = None
    target_region: str | None = Field(None, max_length=128)
    is_active: bool = True

    @field_validator("target_url")
    @classmethod
    def _check_target_url(cls, v: str) -> str:
        return _validate_target_url(v)


class BannerCreate(BannerBase):
    pass


class BannerUpdate(BaseModel):
    image_file_id: uuid.UUID | None = None
    target_url: str | None = Field(None, max_length=2048)
    title: str | None = Field(None, max_length=255)
    placement: BannerPlacement | None = None
    target_animal_type_id: uuid.UUID | None = None
    target_breed_id: uuid.UUID | None = None
    target_region: str | None = Field(None, max_length=128)
    is_active: bool | None = None

    @field_validator("target_url")
    @classmethod
    def _check_target_url(cls, v: str | None) -> str | None:
        return _validate_target_url(v) if v is not None else None


class BannerResponse(BannerBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    campaign_id: uuid.UUID
    impressions_count: int
    clicks_count: int
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------
# /ads/serve
# ---------------------------------------------------------------------


class ServeResponse(BaseModel):
    """
    Минимум того, что нужно фронту для отрисовки баннера.
    Не включаем счётчики/таргетинг — это лишние данные для публичного API.
    """

    banner_id: uuid.UUID
    image_file_id: uuid.UUID | None
    target_url: str
    title: str | None
    placement: BannerPlacement


# ---------------------------------------------------------------------
# /ads/events
# ---------------------------------------------------------------------


class AdEventCreate(BaseModel):
    """
    Тело POST /ads/events. ip/user_agent_hash сервер выводит из заголовков
    запроса — клиенту не доверяем эти поля (легко подделать для накрутки).
    """

    banner_id: uuid.UUID
    event_type: AdEventType
    page_url: str | None = Field(None, max_length=2048)


# ---------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------


class CampaignStats(BaseModel):
    """
    Агрегированная статистика по кампании. CTR — clicks/impressions
    (с защитой от деления на 0 в SQL через NULLIF).
    """

    campaign_id: uuid.UUID
    impressions: int
    clicks: int
    ctr: float
    spent: Decimal
    budget: Decimal
    # remaining_budget = budget - spent. Готовое поле, чтобы фронт
    # не считал сам.
    remaining_budget: Decimal


class DailyStat(BaseModel):
    """Дневная агрегация: дата, показы, клики."""

    day: date
    impressions: int
    clicks: int
