"""
Репозиторий outbox-событий.

Главные операции:
- enqueue: вставить event в той же транзакции, что и бизнес-операция
  (без commit — он делается вызывающим кодом).
- fetch_pending: забрать N pending для воркера (SELECT FOR UPDATE
  SKIP LOCKED — позволяет нескольким воркерам работать параллельно
  без race condition).
- mark_sent / mark_failed: терминальные переходы.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Sequence

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.outbox import OutboxEvent, OutboxStatus


async def enqueue(
    db: AsyncSession,
    *,
    exchange: str | None,
    routing_key: str,
    payload: dict,
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
    )
    db.add(obj)
    await db.flush()
    return obj


async def fetch_pending(
    db: AsyncSession, limit: int = 100
) -> Sequence[OutboxEvent]:
    """
    SELECT FOR UPDATE SKIP LOCKED — берёт строки, минуя те, что
    залочены другими транзакциями. Так несколько worker-инстансов
    могут работать параллельно без race condition: каждый возьмёт
    свой кусок.

    Если воркер один — SKIP LOCKED не вредит, просто работает как
    обычный SELECT FOR UPDATE.
    """
    stmt = (
        select(OutboxEvent)
        .where(OutboxEvent.status == OutboxStatus.pending)
        .order_by(OutboxEvent.created_at.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return (await db.execute(stmt)).scalars().all()


async def mark_sent(db: AsyncSession, event_id: uuid.UUID) -> None:
    stmt = (
        update(OutboxEvent)
        .where(OutboxEvent.id == event_id)
        .values(
            status=OutboxStatus.sent,
            sent_at=datetime.utcnow(),
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
        )
    )
    await db.execute(stmt)
