"""
Репозиторий outbox-событий.

Главные операции:
- enqueue: вставить event в той же транзакции, что и бизнес-операция
  (без commit — он делается вызывающим кодом).
- claim_pending: «застолбить» N pending для воркера (SELECT FOR UPDATE
  SKIP LOCKED — позволяет нескольким воркерам работать параллельно
  без race condition).
- mark_sent / mark_failed: терминальные переходы.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Sequence

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.outbox import OutboxEvent, OutboxStatus


async def enqueue(
    db: AsyncSession,
    *,
    exchange: str | None,
    routing_key: str,
    payload: dict,
    delay: timedelta | None = None,
) -> OutboxEvent:
    """
    Создаёт outbox-запись. БЕЗ commit — вызывающий код коммитит
    транзакцию с основной бизнес-операцией. Это и есть «трансакционный
    outbox»: событие появится в БД тогда и только тогда, когда основная
    операция прошла.
    """
    obj = OutboxEvent(
        exchange=exchange,
        routing_key=routing_key,
        payload=payload,
        # Отложенная публикация (повторы с backoff): claim_pending не берёт
        # событие, пока locked_until в будущем.
        locked_until=(datetime.now(timezone.utc) + delay) if delay else None,
    )
    db.add(obj)
    await db.flush()
    return obj


# Сколько dispatcher держит застолблённую пачку. С запасом больше времени
# публикации пачки; после истечения событие снова доступно (dispatcher упал).
CLAIM_TTL = timedelta(seconds=60)


async def claim_pending(
    db: AsyncSession, limit: int = 100
) -> Sequence[OutboxEvent]:
    """
    «Застолбить» пачку pending-событий: проставить locked_until и вернуть
    их. БЕЗ commit — вызывающий коммитит сразу, до публикации.

    Ревью 2026-10-06, BE-18: FOR UPDATE SKIP LOCKED держал строки только
    до первого commit'а внутри пачки, и второй dispatcher мог опубликовать
    остаток повторно. locked_until переживает commit'ы: второй dispatcher
    такие строки не берёт, пока срок не истёк.
    """
    now = datetime.now(timezone.utc)
    candidates = (
        select(OutboxEvent.id)
        .where(
            OutboxEvent.status == OutboxStatus.pending,
            or_(OutboxEvent.locked_until.is_(None), OutboxEvent.locked_until < now),
        )
        .order_by(OutboxEvent.created_at.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    stmt = (
        update(OutboxEvent)
        .where(OutboxEvent.id.in_(candidates.scalar_subquery()))
        .values(locked_until=now + CLAIM_TTL)
        .returning(OutboxEvent)
        .execution_options(synchronize_session=False)
    )
    rows = (await db.execute(stmt)).scalars().all()
    return sorted(rows, key=lambda e: e.created_at)


async def mark_sent(db: AsyncSession, event_id: uuid.UUID) -> None:
    stmt = (
        update(OutboxEvent)
        .where(OutboxEvent.id == event_id)
        .values(
            status=OutboxStatus.sent,
            sent_at=datetime.now(timezone.utc),
        )
    )
    await db.execute(stmt)


async def mark_failed(
    db: AsyncSession, event_id: uuid.UUID, error: str
) -> None:
    """
    Помечает событие как failed после превышения числа попыток.
    Не делаем delete — failed строки полезны для разбора инцидентов.
    Cleanup старых failed строк — отдельная cron-задача (TODO).
    """
    stmt = (
        update(OutboxEvent)
        .where(OutboxEvent.id == event_id)
        .values(
            status=OutboxStatus.failed,
            last_error=error[:2000],
        )
    )
    await db.execute(stmt)


async def increment_attempts(
    db: AsyncSession, event_id: uuid.UUID, error: str
) -> None:
    """
    Увеличивает счётчик попыток после неудачного publish. Не меняет
    status — событие остаётся pending и попадёт в следующий тик.
    """
    stmt = (
        update(OutboxEvent)
        .where(OutboxEvent.id == event_id)
        .values(
            attempts=OutboxEvent.attempts + 1,
            last_error=error[:2000],
            # Снимаем «застолбление» — следующий тик повторит попытку.
            locked_until=None,
        )
    )
    await db.execute(stmt)
