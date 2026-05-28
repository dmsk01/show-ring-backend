"""
Аналитические запросы (этап 12).

Все запросы — Raw SQL. ORM здесь избыточен:
- сложные подзапросы и FILTER (WHERE) на ORM-уровне читаются хуже,
- raw SQL — естественный инструмент аналитика, ORM усложняет жизнь
  без выигрыша.

Все запросы используют биндинг параметров через `text(... :param)`,
никаких f-string'ов — иначе SQL-injection.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app import redis as redis_module

logger = logging.getLogger(__name__)


# bug_226 audit 2026-05-28: dashboard делает 10 параллельных COUNT(*)
# без WHERE по большим таблицам. На 100k+ строк каждый — 1-2 сек, в
# сумме 10-20 сек на каждое открытие админки. Кеш на 5 минут даёт
# приемлемую «свежесть» (это аналитика, не realtime) и отрезает
# 99% запросов от БД. Версия v1 в ключе — на случай, когда форма
# ответа изменится (добавим поле): достаточно сменить v1→v2, и
# старый кеш протухнет (TTL — fallback).
DASHBOARD_CACHE_KEY = "analytics:dashboard:v1"
DASHBOARD_CACHE_TTL_SECONDS = 5 * 60


# ---------------------------------------------------------------------
# Платформенный дашборд
# ---------------------------------------------------------------------


# Один SELECT с подзапросами — все счётчики за один round-trip к БД.
# Подзапросы в SELECT работают, потому что каждый возвращает скаляр.
DASHBOARD_SQL = text(
    """
    SELECT
        (SELECT COUNT(*) FROM users WHERE is_active) AS total_users,
        (SELECT COUNT(*) FROM kennels WHERE is_verified) AS verified_kennels,
        (SELECT COUNT(*) FROM kennels) AS total_kennels,
        (SELECT COUNT(*) FROM dogs) AS total_dogs,
        (SELECT COUNT(*) FROM breeds) AS total_breeds,
        (SELECT COUNT(*) FROM shows WHERE status = 'completed') AS completed_shows,
        (SELECT COUNT(*) FROM shows WHERE status = 'registration_open') AS open_shows,
        (SELECT COUNT(*) FROM classifieds WHERE status = 'active') AS active_classifieds,
        (SELECT COUNT(*) FROM litters) AS total_litters,
        (SELECT COUNT(*) FROM ad_campaigns WHERE status = 'active') AS active_campaigns
    """
)


async def dashboard(db: AsyncSession) -> dict:
    """
    Сводка платформы — одной строкой.

    Кеш Redis: TTL 5 минут, ключ `analytics:dashboard:v1`. При hit'е
    SELECT не выполняется. При miss'е делаем запрос и сохраняем
    результат с last_updated_at — клиент видит, насколько свежи цифры.

    Fail-open на Redis: если кеш недоступен (нет redis_client или
    Redis отвалился), просто идём в БД без кеша. Это аналитика, не
    идемпотентный платёж — лишнее обращение к PG не ломает корректность,
    только просаживает SLA на одну запросу.
    """
    rc = redis_module.redis_client

    if rc is not None:
        try:
            cached = await rc.get(DASHBOARD_CACHE_KEY)
            if cached:
                return json.loads(cached)
        except Exception:  # noqa: BLE001 — Redis может быть в плохом состоянии
            logger.warning(
                "Redis GET failed for dashboard cache", exc_info=True
            )

    row = (await db.execute(DASHBOARD_SQL)).mappings().one()
    result = dict(row)
    # ISO-string, потому что json не сериализует datetime; pydantic
    # на стороне роутера сам распарсит обратно в datetime.
    result["last_updated_at"] = datetime.now(timezone.utc).isoformat()

    if rc is not None:
        try:
            await rc.set(
                DASHBOARD_CACHE_KEY,
                json.dumps(result),
                ex=DASHBOARD_CACHE_TTL_SECONDS,
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "Redis SET failed for dashboard cache", exc_info=True
            )

    return result


# ---------------------------------------------------------------------
# Топ пород по записям на выставки
# ---------------------------------------------------------------------


TOP_BREEDS_BY_ENTRIES_SQL = text(
    """
    SELECT b.id AS breed_id, b.name AS breed_name,
           COUNT(se.id) AS entries_count
    FROM show_entries se
    JOIN dogs d ON se.dog_id = d.id
    JOIN breeds b ON d.breed_id = b.id
    WHERE se.created_at >= :period_start
    GROUP BY b.id, b.name
    ORDER BY entries_count DESC
    LIMIT :limit_count
    """
)


async def top_breeds_by_entries(
    db: AsyncSession, period_start: datetime, limit: int = 20
) -> list[dict]:
    rows = await db.execute(
        TOP_BREEDS_BY_ENTRIES_SQL,
        {"period_start": period_start, "limit_count": limit},
    )
    return [dict(r) for r in rows.mappings().all()]


# ---------------------------------------------------------------------
# Отчёт по выставке
# ---------------------------------------------------------------------


# FILTER (WHERE …) — PostgreSQL-specific. Считает COUNT/SUM только по
# строкам, удовлетворяющим условию. Альтернатива через CASE WHEN работает
# везде, но менее читаема и медленнее (нужно вычислять CASE для всех строк).
SHOW_REPORT_SQL = text(
    """
    SELECT
        b.name AS breed_name,
        sc.name AS class_name,
        sc.age_from_months AS class_age_from,
        COUNT(se.id) AS entries,
        COUNT(sr.id) FILTER (WHERE sr.is_class_winner) AS cw_count,
        COUNT(sr.id) FILTER (WHERE sr.is_best_of_breed) AS bob_count,
        COUNT(sr.id) FILTER (WHERE sr.placement = 1) AS first_place_count
    FROM show_entries se
    JOIN dogs d ON se.dog_id = d.id
    JOIN breeds b ON d.breed_id = b.id
    JOIN show_classes sc ON se.show_class_id = sc.id
    LEFT JOIN show_results sr ON sr.show_entry_id = se.id
    WHERE se.show_id = :show_id
    GROUP BY b.id, b.name, sc.id, sc.name, sc.age_from_months
    ORDER BY sc.age_from_months, b.name
    """
)


async def show_report(
    db: AsyncSession, show_id: uuid.UUID
) -> list[dict]:
    rows = await db.execute(SHOW_REPORT_SQL, {"show_id": show_id})
    return [dict(r) for r in rows.mappings().all()]


# Оценочная выручка по выставке: entry_fee * число записей.
# Если на этапе биллинга появится is_paid у ShowEntry, заменим на
# SUM(CASE WHEN is_paid THEN entry_fee).
SHOW_REVENUE_SQL = text(
    """
    SELECT
        s.entry_fee,
        COUNT(se.id) AS entries_count,
        COALESCE(s.entry_fee, 0) * COUNT(se.id) AS revenue_estimate
    FROM shows s
    LEFT JOIN show_entries se ON se.show_id = s.id
    WHERE s.id = :show_id
    GROUP BY s.id, s.entry_fee
    """
)


async def show_revenue_estimate(
    db: AsyncSession, show_id: uuid.UUID
) -> dict | None:
    row = (
        await db.execute(SHOW_REVENUE_SQL, {"show_id": show_id})
    ).mappings().first()
    return dict(row) if row else None


# ---------------------------------------------------------------------
# Реклама: дневная статистика по всем кампаниям
# ---------------------------------------------------------------------


# date_trunc('day', …) — стандартный PG-приём для группировки по дням.
# NULLIF — защита от деления на 0 при расчёте CTR.
ADS_DAILY_SQL = text(
    """
    SELECT
        date_trunc('day', ae.created_at)::date AS day,
        COUNT(*) FILTER (WHERE ae.event_type = 'impression') AS impressions,
        COUNT(*) FILTER (WHERE ae.event_type = 'click') AS clicks,
        ROUND(
            COUNT(*) FILTER (WHERE ae.event_type = 'click')::numeric /
            NULLIF(COUNT(*) FILTER (WHERE ae.event_type = 'impression'), 0)
            * 100, 2
        ) AS ctr_percent
    FROM ad_events ae
    WHERE ae.created_at >= :period_start
    GROUP BY 1
    ORDER BY 1 DESC
    """
)


async def ads_daily(
    db: AsyncSession, period_start: datetime
) -> list[dict]:
    rows = await db.execute(
        ADS_DAILY_SQL, {"period_start": period_start}
    )
    return [dict(r) for r in rows.mappings().all()]


# Топ кампаний по доходу (потраченному бюджету).
TOP_CAMPAIGNS_SQL = text(
    """
    SELECT
        c.id, c.name, c.spent, c.budget,
        ROUND(c.spent::numeric / NULLIF(c.budget, 0) * 100, 2) AS spent_percent
    FROM ad_campaigns c
    WHERE c.created_at >= :period_start
    ORDER BY c.spent DESC
    LIMIT :limit_count
    """
)


async def top_campaigns(
    db: AsyncSession, period_start: datetime, limit: int = 10
) -> list[dict]:
    rows = await db.execute(
        TOP_CAMPAIGNS_SQL,
        {"period_start": period_start, "limit_count": limit},
    )
    return [dict(r) for r in rows.mappings().all()]
