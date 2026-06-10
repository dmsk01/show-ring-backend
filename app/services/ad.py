"""
Сервис рекламного модуля (этап 10).

Что внутри:
- CRUD-обёртки с проверкой владельца (advertiser_id == requester или admin).
- record_event — запись события с дедупликацией через Redis SET TTL=60s.
- Атомарное списание бюджета через try_charge_campaign.

Заметка про worker: в полной production-схеме события идут через
RabbitMQ → батч-воркер → INSERT (см. ТЗ stage-10). На этапе 10
делаем синхронный INSERT в API; throughput dev-нагрузки это
выдерживает. Переезд на воркер — этап 14.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import date, datetime, timedelta, timezone

import json

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.ad import AdBanner, AdCampaign, AdEventType, CampaignStatus

# Импортируем МОДУЛЬ, а не значение: init_redis() пере-присваивает
# app.redis.redis_client после импорта. Value-импорт навсегда связал бы
# имя с None — дедупликация молча не работала бы (review 2026-06-10).
from app import redis as redis_state
from app.repositories import ad as repo
from app.services.rabbit import rabbit_service

logger = logging.getLogger(__name__)


# Дедупликация событий: 60-секундное окно. Внутри окна повтор от того же
# (ip + user_agent + banner + тип) считается фродом и не учитывается.
DEDUP_TTL_SECONDS = 60

# Имя очереди должно совпадать с константой в worker/handlers/ad_handler.py.
AD_EVENTS_QUEUE = "ad_events"


async def _publish_event(
    *,
    banner_id: uuid.UUID,
    event_type: AdEventType,
    user_id: uuid.UUID | None,
    ip: str | None,
    ua_hash: str | None,
    page_url: str | None,
) -> None:
    """
    Публикует событие в очередь ad_events. fire-and-forget: при сбое
    RabbitMQ событие потеряется, но это допустимо для аналитики
    (потеря 0.01% impression'ов на падении брокера некритична).

    Если хотим гарантию — добавить outbox pattern (запись в БД +
    отдельный publisher). Это уже не «батч ради скорости».
    """
    payload = {
        "banner_id": str(banner_id),
        "event_type": event_type.value,
        "user_id": str(user_id) if user_id else None,
        "ip": ip,
        "user_agent_hash": ua_hash,
        "page_url": page_url,
    }
    try:
        await rabbit_service.publish(AD_EVENTS_QUEUE, json.dumps(payload))
    except Exception as e:  # noqa: BLE001
        logger.warning("ad_event publish failed: %s", e)


def _hash_user_agent(user_agent: str | None) -> str | None:
    """SHA-256(user_agent) — компактнее хранить и анонимизирует."""
    if not user_agent:
        return None
    return hashlib.sha256(user_agent.encode("utf-8")).hexdigest()


def _ensure_owner(
    campaign: AdCampaign, user_id: uuid.UUID, is_admin: bool
) -> None:
    if campaign.advertiser_id != user_id and not is_admin:
        raise ValueError("forbidden")


# ---------------------------------------------------------------------
# Campaign / banner CRUD
# ---------------------------------------------------------------------


async def create_campaign(
    db: AsyncSession, advertiser_id: uuid.UUID, fields: dict
) -> AdCampaign:
    return await repo.create_campaign(
        db, advertiser_id=advertiser_id, **fields
    )


async def update_campaign(
    db: AsyncSession,
    campaign_id: uuid.UUID,
    user_id: uuid.UUID,
    is_admin: bool,
    fields: dict,
) -> AdCampaign:
    obj = await repo.get_campaign(db, campaign_id)
    if obj is None:
        raise ValueError("not_found")
    _ensure_owner(obj, user_id, is_admin)
    for k, v in fields.items():
        setattr(obj, k, v)
    await db.commit()
    await db.refresh(obj)
    return obj


async def create_banner(
    db: AsyncSession,
    campaign_id: uuid.UUID,
    user_id: uuid.UUID,
    is_admin: bool,
    fields: dict,
) -> AdBanner:
    campaign = await repo.get_campaign(db, campaign_id)
    if campaign is None:
        raise ValueError("campaign_not_found")
    _ensure_owner(campaign, user_id, is_admin)
    return await repo.create_banner(db, campaign_id=campaign_id, **fields)


async def update_banner(
    db: AsyncSession,
    banner_id: uuid.UUID,
    user_id: uuid.UUID,
    is_admin: bool,
    fields: dict,
) -> AdBanner:
    banner = await repo.get_banner(db, banner_id)
    if banner is None:
        raise ValueError("not_found")
    campaign = await repo.get_campaign(db, banner.campaign_id)
    if campaign is None:
        raise ValueError("campaign_not_found")
    _ensure_owner(campaign, user_id, is_admin)
    for k, v in fields.items():
        setattr(banner, k, v)
    await db.commit()
    await db.refresh(banner)
    return banner


async def delete_campaign(
    db: AsyncSession,
    campaign_id: uuid.UUID,
    user_id: uuid.UUID,
    is_admin: bool,
) -> None:
    obj = await repo.get_campaign(db, campaign_id)
    if obj is None:
        raise ValueError("not_found")
    _ensure_owner(obj, user_id, is_admin)
    # CASCADE: удаление кампании уносит её баннеры (ad_banners), а с ними
    # — все их события (ad_events).
    await db.delete(obj)
    await db.commit()


async def delete_banner(
    db: AsyncSession,
    banner_id: uuid.UUID,
    user_id: uuid.UUID,
    is_admin: bool,
) -> None:
    banner = await repo.get_banner(db, banner_id)
    if banner is None:
        raise ValueError("not_found")
    # Право — у владельца кампании баннера (или admin), как в update_banner.
    campaign = await repo.get_campaign(db, banner.campaign_id)
    if campaign is None:
        raise ValueError("campaign_not_found")
    _ensure_owner(campaign, user_id, is_admin)
    # CASCADE уносит события баннера (ad_events).
    await db.delete(banner)
    await db.commit()


# ---------------------------------------------------------------------
# Serve
# ---------------------------------------------------------------------


async def pick_banner(
    db: AsyncSession,
    *,
    placement: str,
    animal_type_id: uuid.UUID | None,
    breed_id: uuid.UUID | None,
    region: str | None,
) -> AdBanner | None:
    return await repo.find_banner_for_serve(
        db,
        placement=placement,
        animal_type_id=animal_type_id,
        breed_id=breed_id,
        region=region,
        today=date.today(),
    )


# ---------------------------------------------------------------------
# Event recording (с дедупликацией)
# ---------------------------------------------------------------------


async def _is_duplicate(
    banner_id: uuid.UUID,
    event_type: AdEventType,
    ip: str | None,
    user_agent_hash: str | None,
) -> bool:
    """
    Redis SET key с TTL=60s. Если SETNX вернул False — ключ уже был,
    значит за последние 60 секунд от этого же (banner + ip + ua) уже
    приходило такое же событие.

    Если Redis недоступен — fail-open: считаем не-дублем. Лучше учесть
    лишнее событие, чем терять статистику при сбое инфры.
    """
    redis_client = redis_state.redis_client
    if redis_client is None:
        return False
    if not ip or not user_agent_hash:
        # Без ip/ua дедупликация невозможна — пропускаем проверку.
        return False
    key = f"ad_dedup:{banner_id}:{ip}:{user_agent_hash}:{event_type.value}"
    try:
        # NX=True + EX=60: ставим ключ, только если его нет; TTL 60 сек.
        # set возвращает True (поставили) или None (был — дубль).
        was_set = await redis_client.set(key, "1", nx=True, ex=DEDUP_TTL_SECONDS)
        return not was_set
    except Exception as e:  # noqa: BLE001 — fail-open при сбое Redis
        logger.warning("Redis dedup failed: %s", e)
        return False


async def record_event(
    db: AsyncSession,
    *,
    banner_id: uuid.UUID,
    event_type: AdEventType,
    user_id: uuid.UUID | None,
    ip: str | None,
    user_agent: str | None,
    page_url: str | None,
) -> bool:
    """
    Записывает событие. Возвращает True если событие учтено, False —
    если отброшено как дубль.

    Два режима (settings.ad_events_async):
    - sync (по умолчанию): сразу пишем в БД из API. Простой dev-путь.
    - async: publish'им в очередь RabbitMQ, batch INSERT делает воркер
      (см. worker/handlers/ad_handler.py). Даёт суб-миллисекундный
      response time на /ads/events при высоком трафике.

    Шаги синхронного пути (всё в одной транзакции, кроме Redis):
    1. Проверяем дедуп через Redis (внетранзакционно — Redis отдельный
       store).
    2. Загружаем баннер + кампанию.
    3. Для impression — пробуем списать бюджет; если не вышло, событие
       всё равно регистрируем, но не платное.
    4. INSERT в ad_events.
    5. UPDATE счётчиков на баннере.
    """
    ua_hash = _hash_user_agent(user_agent)

    if await _is_duplicate(banner_id, event_type, ip, ua_hash):
        return False

    # Если включён async-режим, не делаем БД-чтений в API: воркер
    # сам подгрузит banner/campaign перед батч-вставкой. Это даёт
    # суб-миллисекундный response time на /ads/events.
    if settings.ad_events_async:
        await _publish_event(
            banner_id=banner_id,
            event_type=event_type,
            user_id=user_id,
            ip=ip,
            ua_hash=ua_hash,
            page_url=page_url,
        )
        return True

    banner = await repo.get_banner(db, banner_id)
    if banner is None:
        raise ValueError("banner_not_found")
    if not banner.is_active:
        raise ValueError("banner_inactive")

    campaign = await repo.get_campaign(db, banner.campaign_id)
    if campaign is None:
        raise ValueError("campaign_not_found")

    # bug_214 audit 2026-05-28: schema требует budget>0 при создании,
    # но через прямой UPDATE/миграцию/админский reset кампания может
    # оказаться с budget<=0 или в нерабочем статусе. До этого фикса
    # запись событий продолжалась бесконечно (особенно при
    # cost_per_impression=0 — try_charge не пытался списать) →
    # «бесплатная» открутка и накрутка счётчиков. Fail-closed: для
    # неактивных или пустых кампаний событие считается отброшенным.
    if campaign.status != CampaignStatus.active or campaign.budget <= 0:
        return False

    # Списываем бюджет только за impression. Клики на этапе 10
    # не тарифицируются — CPM-модель. CPC расширим в будущем.
    if event_type == AdEventType.impression and campaign.cost_per_impression > 0:
        charged = await repo.try_charge_campaign(
            db, campaign.id, campaign.cost_per_impression
        )
        if not charged:
            # Бюджет исчерпан — событие записываем (для аналитики), но
            # дальше показывать не будем (campaign перейдёт в completed).
            await repo.auto_complete_campaign_if_exhausted(db, campaign.id)

    await repo.record_event(
        db,
        banner_id=banner_id,
        event_type=event_type,
        user_id=user_id,
        ip=ip,
        user_agent_hash=ua_hash,
        page_url=page_url,
    )
    await repo.increment_banner_counter(db, banner_id, event_type)
    await db.commit()
    return True


# ---------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------


async def stats_for_campaign(
    db: AsyncSession,
    campaign_id: uuid.UUID,
    user_id: uuid.UUID,
    is_admin: bool,
) -> dict:
    """
    Возвращает агрегированную статистику с проверкой владельца:
    cm.advertiser_id == user_id (или admin).
    """
    campaign = await repo.get_campaign(db, campaign_id)
    if campaign is None:
        raise ValueError("not_found")
    _ensure_owner(campaign, user_id, is_admin)

    impressions, clicks = await repo.campaign_totals(db, campaign_id)
    ctr = (clicks / impressions) if impressions > 0 else 0.0
    return {
        "campaign_id": campaign.id,
        "impressions": impressions,
        "clicks": clicks,
        "ctr": round(ctr, 4),
        "spent": campaign.spent,
        "budget": campaign.budget,
        "remaining_budget": campaign.budget - campaign.spent,
    }


async def daily_stats(
    db: AsyncSession,
    campaign_id: uuid.UUID,
    user_id: uuid.UUID,
    is_admin: bool,
    days: int = 30,
) -> list[dict]:
    campaign = await repo.get_campaign(db, campaign_id)
    if campaign is None:
        raise ValueError("not_found")
    _ensure_owner(campaign, user_id, is_admin)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    return await repo.daily_stats(db, campaign_id, start, end)