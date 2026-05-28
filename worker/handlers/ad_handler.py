"""
Worker для рекламных событий (этап 14 follow-up для этапа 10).

Архитектура:
- API публикует событие в очередь `ad_events`. Это занимает миллисекунды
  и не блокирует ответ клиенту.
- Этот воркер собирает события в батч (по таймауту или размеру) и
  выполняет один multi-row INSERT + один UPDATE на каждый банер.

Зачем батчинг:
- При высоком трафике (1000+ events/sec) сэкономим раунд-трипы к PG.
- Один UPDATE с +N счётчика дешевле N отдельных UPDATE'ов.
- Atomic списание бюджета остаётся conditional UPDATE — race-condition-safe.

Дедупликация (Redis SETNX) делается ДО publish'а на стороне API,
чтобы битые/повторные события не попадали в очередь.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections import defaultdict
from typing import Iterable

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ad import AdBanner, AdEvent, AdEventType
from app.repositories import ad as ad_repo

logger = logging.getLogger(__name__)


AD_EVENTS_QUEUE = "ad_events"


# Параметры батча. На dev-нагрузке оставляем маленькими — иначе при
# тестировании пришлось бы накидывать сотни запросов, чтобы увидеть
# INSERT. В prod увеличиваются через env (не сейчас).
BATCH_SIZE = 50
BATCH_TIMEOUT_SECONDS = 2.0


async def process_batch(
    db: AsyncSession, batch: list[dict]
) -> None:
    """
    Выполняет батч событий: bulk INSERT + per-banner counter UPDATE
    + conditional charge campaign. Всё в одной транзакции — либо весь
    батч записался, либо ничего.

    batch — список dict'ов с полями event_create:
        {banner_id, event_type, user_id?, ip?, user_agent_hash?, page_url?}
    """
    if not batch:
        return

    # 1. Bulk INSERT через add_all — SQLAlchemy сделает один INSERT
    # … VALUES (...), (...), ... вместо N отдельных.
    objs = []
    for raw in batch:
        objs.append(
            AdEvent(
                banner_id=uuid.UUID(raw["banner_id"]),
                event_type=AdEventType(raw["event_type"]),
                user_id=uuid.UUID(raw["user_id"]) if raw.get("user_id") else None,
                ip=raw.get("ip"),
                user_agent_hash=raw.get("user_agent_hash"),
                page_url=raw.get("page_url"),
            )
        )
    db.add_all(objs)

    # 2. Группируем счётчики по (banner_id, event_type) и одним UPDATE
    # инкрементируем на N.
    counts: dict[tuple[uuid.UUID, AdEventType], int] = defaultdict(int)
    for o in objs:
        counts[(o.banner_id, o.event_type)] += 1
    for (banner_id, event_type), n in counts.items():
        field = (
            AdBanner.impressions_count
            if event_type == AdEventType.impression
            else AdBanner.clicks_count
        )
        await db.execute(
            update(AdBanner)
            .where(AdBanner.id == banner_id)
            .values({field: field + n})
        )

    # 3. Атомарное списание бюджета — только для impression'ов. Группируем
    # по banner_id чтобы списать × N за один UPDATE.
    impressions_per_banner: dict[uuid.UUID, int] = defaultdict(int)
    for o in objs:
        if o.event_type == AdEventType.impression:
            impressions_per_banner[o.banner_id] += 1
    for banner_id, n in impressions_per_banner.items():
        banner = await ad_repo.get_banner(db, banner_id)
        if banner is None:
            continue
        campaign = await ad_repo.get_campaign(db, banner.campaign_id)
        if campaign is None or campaign.cost_per_impression <= 0:
            continue
        total_cost = campaign.cost_per_impression * n
        charged = await ad_repo.try_charge_campaign(
            db, campaign.id, total_cost
        )
        if not charged:
            await ad_repo.auto_complete_campaign_if_exhausted(db, campaign.id)

    await db.commit()
    logger.info("ad_events batch: %d events processed", len(batch))


# ---------------------------------------------------------------------
# Аккумулятор: собирает events, флашит по таймауту или размеру
# ---------------------------------------------------------------------


class BatchAccumulator:
    """
    Собирает события в список. process_batch вызывается:
    - когда накопилось BATCH_SIZE,
    - или когда прошёл BATCH_TIMEOUT_SECONDS с момента первого события.

    Реализация: фоновая задача periodic_flush + флаш по count в add().
    Lock защищает от race condition между add и periodic_flush.
    """

    def __init__(self, session_factory):
        self._buffer: list[dict] = []
        self._lock = asyncio.Lock()
        self._session_factory = session_factory
        self._flush_task: asyncio.Task | None = None

    def start(self) -> None:
        if self._flush_task is None:
            self._flush_task = asyncio.create_task(self._periodic_flush())

    async def stop(self) -> None:
        """
        bug_235 audit 2026-05-28: graceful shutdown. Раньше при
        SIGTERM воркер просто закрывал RabbitMQ-соединение и выходил,
        пока _periodic_flush спал на asyncio.sleep — фоновая задача
        отменялась через cancel of event loop, а буфер с событиями в
        памяти (до BATCH_SIZE-1 штук) терялся. Биллинг рекламы
        недосчитывался показов.
        Тут: отменяем периодический flush, дожидаемся его выхода и
        сливаем оставшийся буфер в БД.
        """
        if self._flush_task is not None:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except (asyncio.CancelledError, Exception):
                # CancelledError ожидаем; любые другие — уже залогированы
                # внутри _periodic_flush через _flush()'а logger.exception.
                pass
            self._flush_task = None
        async with self._lock:
            if self._buffer:
                batch, self._buffer = self._buffer, []
            else:
                batch = None
        if batch:
            await self._flush(batch)

    async def add(self, event: dict) -> None:
        async with self._lock:
            self._buffer.append(event)
            if len(self._buffer) >= BATCH_SIZE:
                batch, self._buffer = self._buffer, []
            else:
                batch = None
        # Флаш ВНЕ lock — БД-операции могут быть долгими, нельзя
        # блокировать новые add().
        if batch:
            await self._flush(batch)

    async def _periodic_flush(self) -> None:
        while True:
            await asyncio.sleep(BATCH_TIMEOUT_SECONDS)
            async with self._lock:
                if self._buffer:
                    batch, self._buffer = self._buffer, []
                else:
                    batch = None
            if batch:
                await self._flush(batch)

    async def _flush(self, batch: list[dict]) -> None:
        async with self._session_factory() as db:
            try:
                await process_batch(db, batch)
            except Exception:
                logger.exception(
                    "ad_events batch flush failed (size=%d)", len(batch)
                )


# Глобальный аккумулятор — стартует в worker/main.py.
_accumulator: BatchAccumulator | None = None


def init_accumulator(session_factory) -> BatchAccumulator:
    global _accumulator
    if _accumulator is None:
        _accumulator = BatchAccumulator(session_factory)
        _accumulator.start()
    return _accumulator


async def on_ad_event_message(body: str) -> None:
    """Парсит JSON и добавляет в батч."""
    if _accumulator is None:
        logger.error("ad_event received but accumulator not initialised")
        return
    try:
        data = json.loads(body)
    except ValueError as e:
        logger.warning("Bad ad_event payload: %s (%s)", body, e)
        return
    await _accumulator.add(data)
