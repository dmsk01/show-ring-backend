"""
Сроки хранения из Политики конфиденциальности (раздел 11):
- журнал безопасности (IP, User-Agent) — 1 год;
- IP / хэш User-Agent / user_id в событиях рекламы — 6 месяцев, затем
  обезличивание (агрегаты показов/кликов остаются).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app.models.ad import (
    AdBanner,
    AdCampaign,
    AdEvent,
    AdEventType,
    BannerPlacement,
)
from app.models.security_audit import SecurityAuditLog
from app.services.retention import purge_expired_personal_data
from tests.integration.checkin_helpers import make_db_user

NOW = datetime(2026, 10, 4, 3, 30, tzinfo=timezone.utc)


async def test_retention_purges_and_anonymizes(db_session):
    user = await make_db_user(db_session)
    old_log = SecurityAuditLog(
        user_id=user.id, action="password_changed", ip="1.2.3.4",
        created_at=NOW - timedelta(days=366),
    )
    fresh_log = SecurityAuditLog(
        user_id=user.id, action="password_changed", ip="1.2.3.4",
        created_at=NOW - timedelta(days=30),
    )
    campaign = AdCampaign(
        advertiser_id=user.id, name="c", budget=Decimal("100"),
        date_start=date(2026, 1, 1), date_end=date(2026, 12, 31),
    )
    db_session.add_all([old_log, fresh_log, campaign])
    await db_session.flush()
    banner = AdBanner(
        campaign_id=campaign.id, target_url="https://example.com",
        placement=BannerPlacement.sidebar,
    )
    db_session.add(banner)
    await db_session.flush()
    old_event = AdEvent(
        banner_id=banner.id, event_type=AdEventType.click, user_id=user.id,
        ip="5.6.7.8", user_agent_hash="h" * 64,
        created_at=NOW - timedelta(days=200),
    )
    fresh_event = AdEvent(
        banner_id=banner.id, event_type=AdEventType.click, user_id=user.id,
        ip="5.6.7.8", user_agent_hash="h" * 64,
        created_at=NOW - timedelta(days=10),
    )
    db_session.add_all([old_event, fresh_event])
    await db_session.flush()
    ids = (old_log.id, fresh_log.id, old_event.id, fresh_event.id)

    stats = await purge_expired_personal_data(db_session, now=NOW)

    assert stats == {"security_logs_deleted": 1, "ad_events_anonymized": 1}
    db_session.expire_all()
    assert await db_session.get(SecurityAuditLog, ids[0]) is None
    assert await db_session.get(SecurityAuditLog, ids[1]) is not None
    old = await db_session.get(AdEvent, ids[2])
    assert old is not None  # событие остаётся для статистики
    assert (old.ip, old.user_agent_hash, old.user_id) == (None, None, None)
    fresh = await db_session.get(AdEvent, ids[3])
    assert fresh.ip == "5.6.7.8"


async def test_retention_is_idempotent(db_session):
    await purge_expired_personal_data(db_session, now=NOW)
    stats = await purge_expired_personal_data(db_session, now=NOW)
    assert stats["ad_events_anonymized"] == 0
    assert stats["security_logs_deleted"] == 0
