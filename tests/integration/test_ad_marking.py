"""
Маркировка интернет-рекламы (ст. 18.1 Федерального закона «О рекламе»).

Реклама должна содержать пометку «Реклама», сведения о рекламодателе и
идентификатор рекламы (erid), полученный через ОРД. Баннер без erid
/ads/serve не отдаёт вовсе — немаркированную рекламу показать нельзя.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from app.models.ad import AdBanner, AdCampaign, BannerPlacement, CampaignStatus
from tests.integration.checkin_helpers import make_db_user


async def _banner(db_session, *, erid: str | None) -> AdBanner:
    user = await make_db_user(db_session)
    campaign = AdCampaign(
        advertiser_id=user.id,
        name="c",
        budget=Decimal("100"),
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
        erid=erid,
    )
    db_session.add(banner)
    await db_session.commit()
    return banner


async def test_unmarked_banner_is_not_served(client, db_session):
    await _banner(db_session, erid=None)
    r = await client.get("/ads/serve", params={"placement": "footer"})
    assert r.status_code == 200
    assert r.json() is None


async def test_marked_banner_carries_label_and_advertiser(client, db_session):
    banner = await _banner(db_session, erid="2SDnjcVm7Ka")
    r = await client.get("/ads/serve", params={"placement": "footer"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["banner_id"] == str(banner.id)
    assert body["label"] == "Реклама"
    assert body["erid"] == "2SDnjcVm7Ka"
    assert body["advertiser_name"] == "ООО «Корма»"
    assert body["advertiser_inn"] == "7700000000"
