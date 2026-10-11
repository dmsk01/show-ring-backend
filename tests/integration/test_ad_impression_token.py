"""
Подписанный токен показа рекламы (ревью 2026-10-06, BE-25).

Раньше /ads/events принимал любой banner_id: показы и клики (а с ними
списание бюджета чужой кампании) накручивались простыми запросами — от
ротации IP и User-Agent спасали только лимиты. Теперь /ads/serve выдаёт
вместе с баннером подписанный токен, событие без валидного токена этого
баннера не учитывается, а каждый токен засчитывается один раз на тип.
"""

from __future__ import annotations

import time
import uuid
from datetime import date, timedelta
from decimal import Decimal

from app.models.ad import AdBanner, AdCampaign, BannerPlacement, CampaignStatus
from app.utils import ad_token
from tests.integration.checkin_helpers import make_db_user


async def _banner(db_session) -> AdBanner:
    user = await make_db_user(db_session)
    campaign = AdCampaign(
        advertiser_id=user.id,
        name="c",
        budget=Decimal("100"),
        cost_per_impression=Decimal("0.01"),
        status=CampaignStatus.active,
        date_start=date.today() - timedelta(days=1),
        date_end=date.today() + timedelta(days=1),
        advertiser_name="ООО «Корма»",
        advertiser_inn="7700000000",
    )
    db_session.add(campaign)
    await db_session.flush()
    banner = AdBanner(
        campaign_id=campaign.id,
        target_url="https://example.com",
        placement=BannerPlacement.footer,
        erid="2SDnjcVm7Ka",
    )
    db_session.add(banner)
    await db_session.commit()
    return banner


async def _served_token(client) -> str:
    r = await client.get("/ads/serve", params={"placement": "footer"})
    assert r.status_code == 200, r.text
    return r.json()["impression_token"]


async def test_serve_returns_token_and_event_with_it_is_accepted(client, db_session):
    banner = await _banner(db_session)
    token = await _served_token(client)
    r = await client.post(
        "/ads/events",
        json={"banner_id": str(banner.id), "event_type": "impression", "impression_token": token},
    )
    assert r.status_code == 202, r.text
    assert r.json()["accepted"] is True


async def test_event_without_token_is_rejected(client, db_session):
    banner = await _banner(db_session)
    r = await client.post(
        "/ads/events", json={"banner_id": str(banner.id), "event_type": "impression"}
    )
    assert r.status_code == 422, r.text


async def test_forged_token_is_rejected(client, db_session):
    banner = await _banner(db_session)
    forged = ad_token.make_token(banner.id, key=b"not-the-server-key")
    r = await client.post(
        "/ads/events",
        json={"banner_id": str(banner.id), "event_type": "impression", "impression_token": forged},
    )
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "invalid_impression_token"


async def test_token_of_another_banner_is_rejected(client, db_session):
    banner = await _banner(db_session)
    other = ad_token.make_token(uuid.uuid4())
    r = await client.post(
        "/ads/events",
        json={"banner_id": str(banner.id), "event_type": "impression", "impression_token": other},
    )
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "invalid_impression_token"


async def test_expired_token_is_rejected(client, db_session):
    banner = await _banner(db_session)
    old = ad_token.make_token(
        banner.id, issued_at=int(time.time()) - ad_token.TTL_SECONDS - 5
    )
    r = await client.post(
        "/ads/events",
        json={"banner_id": str(banner.id), "event_type": "impression", "impression_token": old},
    )
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "invalid_impression_token"


async def test_token_counts_once_per_event_type(client, db_session):
    banner = await _banner(db_session)
    token = await _served_token(client)

    def body(event_type: str) -> dict:
        return {"banner_id": str(banner.id), "event_type": event_type, "impression_token": token}

    first = await client.post("/ads/events", json=body("impression"))
    # Другой IP/UA не помогает: повтор того же токена не учитывается.
    again = await client.post(
        "/ads/events", json=body("impression"), headers={"User-Agent": "other-bot"}
    )
    click = await client.post("/ads/events", json=body("click"))
    assert first.json()["accepted"] is True
    assert again.json()["accepted"] is False
    assert click.json()["accepted"] is True


def test_token_roundtrip_and_tamper():
    banner_id = uuid.uuid4()
    token = ad_token.make_token(banner_id)
    parsed_banner, _nonce = ad_token.parse_token(token)
    assert parsed_banner == banner_id
    tampered = token[:-2] + ("AA" if not token.endswith("AA") else "BB")
    try:
        ad_token.parse_token(tampered)
    except ad_token.InvalidAdToken:
        pass
    else:
        raise AssertionError("tampered token accepted")
