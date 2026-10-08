"""
Интеграция: учёт рекламных событий (ревью 2026-10-06, BE-06 и BE-25).

BE-06 (async-режим, AD_EVENTS_ASYNC=true):
- API не публикует события несуществующих баннеров и кампаний не в показе
  (раньше один запрос с выдуманным banner_id ронял весь батч воркера на
  FK, батч крутился в повторах, а при переполнении буфера терялись
  валидные события);
- воркер не валит батч из-за битого события и не списывает деньги у
  кампаний на паузе;
- если остатка бюджета не хватает на весь батч, списывается остаток,
  а не ноль.
BE-25: дедупликация работает и без User-Agent (раньше curl без UA обходил
её полностью).
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.ad import (
    AdBanner,
    AdCampaign,
    AdEvent,
    AdEventType,
    BannerPlacement,
    CampaignStatus,
)
from app.services import ad as ad_svc
from app.services.rabbit import rabbit_service
from tests.integration.checkin_helpers import make_db_user
from worker.handlers.ad_handler import process_batch


async def _banner(
    db_session, *, status=CampaignStatus.active, budget="100", spent="0", cpi="0.01"
) -> AdBanner:
    user = await make_db_user(db_session)
    campaign = AdCampaign(
        advertiser_id=user.id, name="c", budget=Decimal(budget),
        spent=Decimal(spent), cost_per_impression=Decimal(cpi), status=status,
        date_start=date.today() - timedelta(days=1),
        date_end=date.today() + timedelta(days=1),
    )
    db_session.add(campaign)
    await db_session.flush()
    banner = AdBanner(
        campaign_id=campaign.id, target_url="https://example.com",
        placement=BannerPlacement.footer, erid="erid-1",
    )
    db_session.add(banner)
    await db_session.commit()
    return banner


@pytest.fixture
def async_mode(monkeypatch):
    monkeypatch.setattr(settings, "ad_events_async", True)
    publish = AsyncMock()
    monkeypatch.setattr(rabbit_service, "publish", publish)
    return publish


async def _record(db_session, banner_id, **kw):
    params = dict(
        banner_id=banner_id, event_type=AdEventType.impression, user_id=None,
        ip="203.0.113.9", user_agent="UA", page_url=None,
    )
    params.update(kw)
    return await ad_svc.record_event(db_session, **params)


async def test_async_mode_rejects_unknown_banner(db_session, async_mode):
    with pytest.raises(ValueError, match="banner_not_found"):
        await _record(db_session, uuid.uuid4())
    async_mode.assert_not_called()


async def test_async_mode_skips_paused_campaign(db_session, async_mode):
    banner = await _banner(db_session, status=CampaignStatus.paused)
    assert await _record(db_session, banner.id) is False
    async_mode.assert_not_called()


async def test_batch_with_unknown_banner_keeps_valid_events(db_session):
    banner = await _banner(db_session)
    batch = [
        {"banner_id": str(uuid.uuid4()), "event_type": "impression"},
        {"banner_id": str(banner.id), "event_type": "impression"},
    ]
    await process_batch(db_session, batch)
    rows = (
        await db_session.execute(select(AdEvent).where(AdEvent.banner_id == banner.id))
    ).scalars().all()
    assert len(rows) == 1


async def test_batch_does_not_charge_paused_campaign(db_session):
    banner = await _banner(db_session, status=CampaignStatus.paused)
    await process_batch(
        db_session, [{"banner_id": str(banner.id), "event_type": "impression"}]
    )
    campaign = await db_session.get(AdCampaign, banner.campaign_id)
    await db_session.refresh(campaign)
    assert campaign.spent == Decimal("0")


async def test_batch_charges_remaining_budget(db_session):
    banner = await _banner(db_session, budget="1.00", spent="0.98", cpi="0.01")
    batch = [{"banner_id": str(banner.id), "event_type": "impression"}] * 5
    await process_batch(db_session, batch)
    campaign = await db_session.get(AdCampaign, banner.campaign_id)
    await db_session.refresh(campaign)
    assert campaign.spent == Decimal("1.00")
    assert campaign.status == CampaignStatus.completed


async def test_dedup_without_user_agent(db_session, test_redis, monkeypatch):
    monkeypatch.setattr("app.redis.redis_client", test_redis)
    banner = await _banner(db_session)
    assert await _record(db_session, banner.id, user_agent=None) is True
    assert await _record(db_session, banner.id, user_agent=None) is False
