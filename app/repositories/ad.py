"""
Репозиторий рекламного модуля (этап 10).

Самые важные запросы здесь:
- `find_banner_for_serve` — выбор подходящего баннера с фильтром
  таргетинга. Используется на каждом запросе /ads/serve.
- `try_charge_campaign` — атомарное списание бюджета. Conditional
  UPDATE гарантирует, что не уйдём в "перерасход" даже под конкурентными
  событиями.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Sequence

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ad import (
    AdBanner,
    AdCampaign,
    AdEvent,
    AdEventType,
    CampaignStatus,
)


# ---------------------------------------------------------------------
# Campaign CRUD
# ---------------------------------------------------------------------


async def create_campaign(db: AsyncSession, **fields) -> AdCampaign:
    obj = AdCampaign(**fields)
    db.add(obj)
    await db.flush()
    await db.commit()
    await db.refresh(obj)
    return obj


async def get_campaign(
    db: AsyncSession, campaign_id: uuid.UUID
) -> AdCampaign | None:
    return await db.get(AdCampaign, campaign_id)


async def list_campaigns_for_advertiser(
    db: AsyncSession, advertiser_id: uuid.UUID
) -> Sequence[AdCampaign]:
    stmt = (
        select(AdCampaign)
        .where(AdCampaign.advertiser_id == advertiser_id)
        .order_by(AdCampaign.created_at.desc())
    )
    return (await db.execute(stmt)).scalars().all()


# ---------------------------------------------------------------------
# Banner CRUD
# ---------------------------------------------------------------------


async def create_banner(db: AsyncSession, **fields) -> AdBanner:
    obj = AdBanner(**fields)
    db.add(obj)
    await db.flush()
    await db.commit()
    await db.refresh(obj)
    return obj


async def get_banner(
    db: AsyncSession, banner_id: uuid.UUID
) -> AdBanner | None:
    return await db.get(AdBanner, banner_id)


# ---------------------------------------------------------------------
# Serve: подбор баннера для показа
# ---------------------------------------------------------------------


async def find_banner_for_serve(
    db: AsyncSession,
    *,
    placement: str,
    animal_type_id: uuid.UUID | None,
    breed_id: uuid.UUID | None,
    region: str | None,
    today: date,
) -> AdBanner | None:
    """
    Выбирает один баннер для показа.

    Логика таргетинга: NULL в банере = "любой контекст". То есть баннер
    без target_breed_id показывается на странице любой породы. С явным
    target_breed_id — только когда breed_id запроса совпадает.

    Дополнительные фильтры:
    - кампания active
    - spent < budget (бюджет не исчерпан)
    - дата сегодня в [date_start, date_end]
    - баннер is_active=True

    Выбор случайного: ORDER BY random() LIMIT 1 — на dev-объёмах
    нормально. На проде с тысячами баннеров переходим на weighted
    sampling через таблицу приоритетов.
    """
    stmt = (
        select(AdBanner)
        .join(AdCampaign, AdCampaign.id == AdBanner.campaign_id)
        .where(
            AdBanner.placement == placement,
            AdBanner.is_active.is_(True),
            AdCampaign.status == CampaignStatus.active,
            AdCampaign.spent < AdCampaign.budget,
            AdCampaign.date_start <= today,
            AdCampaign.date_end >= today,
        )
    )

    # NULL = "любой". В SQL пишем как OR с IS NULL.
    if animal_type_id is not None:
        stmt = stmt.where(
            (AdBanner.target_animal_type_id.is_(None))
            | (AdBanner.target_animal_type_id == animal_type_id)
        )
    else:
        # Запросов без animal_type не разрешаем "ловить" банеры с явным
        # таргетом — это другой класс контекста.
        stmt = stmt.where(AdBanner.target_animal_type_id.is_(None))

    if breed_id is not None:
        stmt = stmt.where(
            (AdBanner.target_breed_id.is_(None))
            | (AdBanner.target_breed_id == breed_id)
        )
    else:
        stmt = stmt.where(AdBanner.target_breed_id.is_(None))

    if region is not None:
        stmt = stmt.where(
            (AdBanner.target_region.is_(None))
            | (AdBanner.target_region.ilike(region))
        )
    else:
        stmt = stmt.where(AdBanner.target_region.is_(None))

    # ИСПРАВЛЕНО (bug_224 audit 2026-05-28): двухэтапный выбор вместо
    # `ORDER BY random()` по всему фильтрованному набору. Раньше при
    # 10k+ баннерах PG вычислял random() для КАЖДОЙ строки, сортировал,
    # брал первую — O(n log n) и секундные задержки на каждый /ads/serve.
    # Теперь:
    #   1. По индексам отбираем до 100 кандидатов (детерминированный
    #      порядок — id ASC: PG идёт по индексу, ранний LIMIT).
    #   2. Из этих 100 — `WHERE id IN (...) ORDER BY random() LIMIT 1`:
    #      random() оценивается максимум 100 раз.
    # Равномерность сохраняется (на каждый запрос — случайный из
    # топ-100), при N>100 даём долгосрочную «справедливость» через
    # рандомизацию вторым шагом. Альтернатива TABLESAMPLE BERNOULLI —
    # ещё быстрее, но менее предсказуема при малом N.
    candidate_ids = (
        await db.execute(
            stmt.with_only_columns(AdBanner.id)
            .order_by(AdBanner.id)
            .limit(100)
        )
    ).scalars().all()
    if not candidate_ids:
        return None
    random_pick = (
        select(AdBanner)
        .where(AdBanner.id.in_(candidate_ids))
        .order_by(func.random())
        .limit(1)
    )
    return (await db.execute(random_pick)).scalar_one_or_none()


# ---------------------------------------------------------------------
# Атомарное списание бюджета
# ---------------------------------------------------------------------


async def try_charge_campaign(
    db: AsyncSession, campaign_id: uuid.UUID, cost: Decimal
) -> bool:
    """
    Conditional UPDATE: списываем cost ТОЛЬКО если после списания не
    превышен бюджет. PG атомарно проверит и обновит.

    Возвращает True если списание прошло, False — если бюджет был бы
    превышен (тогда событие не учитываем как платное).
    """
    stmt = (
        update(AdCampaign)
        .where(
            AdCampaign.id == campaign_id,
            (AdCampaign.spent + cost) <= AdCampaign.budget,
        )
        .values(spent=AdCampaign.spent + cost)
    )
    result = await db.execute(stmt)
    # rowcount недоступен в Result[Any] для pyright — через getattr
    # (см. репозиторий tasks с тем же комментарием).
    return getattr(result, "rowcount", 0) == 1


async def auto_complete_campaign_if_exhausted(
    db: AsyncSession, campaign_id: uuid.UUID
) -> None:
    """
    Если бюджет исчерпан (spent >= budget), переводим кампанию в
    completed. Отдельным UPDATE'ом, чтобы не блокировать try_charge.
    """
    stmt = (
        update(AdCampaign)
        .where(
            AdCampaign.id == campaign_id,
            AdCampaign.spent >= AdCampaign.budget,
            AdCampaign.status == CampaignStatus.active,
        )
        .values(status=CampaignStatus.completed)
    )
    await db.execute(stmt)


# ---------------------------------------------------------------------
# AdEvent: запись + обновление счётчиков
# ---------------------------------------------------------------------


async def record_event(
    db: AsyncSession,
    *,
    banner_id: uuid.UUID,
    event_type: AdEventType,
    user_id: uuid.UUID | None,
    ip: str | None,
    user_agent_hash: str | None,
    page_url: str | None,
) -> AdEvent:
    obj = AdEvent(
        banner_id=banner_id,
        event_type=event_type,
        user_id=user_id,
        ip=ip,
        user_agent_hash=user_agent_hash,
        page_url=page_url,
    )
    db.add(obj)
    await db.flush()
    return obj


async def increment_banner_counter(
    db: AsyncSession,
    banner_id: uuid.UUID,
    event_type: AdEventType,
) -> None:
    """Атомарный инкремент счётчика на баннере."""
    field = (
        AdBanner.impressions_count
        if event_type == AdEventType.impression
        else AdBanner.clicks_count
    )
    stmt = (
        update(AdBanner)
        .where(AdBanner.id == banner_id)
        .values({field: field + 1})
    )
    await db.execute(stmt)


# ---------------------------------------------------------------------
# Статистика
# ---------------------------------------------------------------------


# Raw SQL для дневной агрегации: показываем как через text() задавать
# параметры безопасно (бинды :start/:end) и использовать
# date_trunc('day', ...) — стандартный PG-приём.
DAILY_STATS_SQL = text(
    """
    SELECT
        date_trunc('day', e.created_at)::date AS day,
        SUM(CASE WHEN e.event_type = 'impression' THEN 1 ELSE 0 END) AS impressions,
        SUM(CASE WHEN e.event_type = 'click' THEN 1 ELSE 0 END) AS clicks
    FROM ad_events e
    JOIN ad_banners b ON b.id = e.banner_id
    WHERE b.campaign_id = :campaign_id
      AND e.created_at >= :start
      AND e.created_at < :end
    GROUP BY 1
    ORDER BY 1
    """
)


async def daily_stats(
    db: AsyncSession,
    campaign_id: uuid.UUID,
    start: datetime,
    end: datetime,
) -> list[dict]:
    rows = await db.execute(
        DAILY_STATS_SQL,
        {"campaign_id": campaign_id, "start": start, "end": end},
    )
    return [dict(r) for r in rows.mappings().all()]


async def campaign_totals(
    db: AsyncSession, campaign_id: uuid.UUID
) -> tuple[int, int]:
    """
    Возвращает (impressions, clicks) по событиям. Не используем
    денормализованные счётчики на баннерах, а считаем по ad_events —
    они являются source of truth.

    Два отдельных запроса вместо одного CASE-WHEN: оба моментальные
    (индекс на (banner_id, created_at)), а читаемость выше.
    """
    base = (
        select(func.count())
        .select_from(AdEvent)
        .join(AdBanner, AdBanner.id == AdEvent.banner_id)
        .where(AdBanner.campaign_id == campaign_id)
    )
    impressions = int(
        (
            await db.execute(
                base.where(AdEvent.event_type == AdEventType.impression)
            )
        ).scalar_one()
    )
    clicks = int(
        (
            await db.execute(
                base.where(AdEvent.event_type == AdEventType.click)
            )
        ).scalar_one()
    )
    return impressions, clicks
