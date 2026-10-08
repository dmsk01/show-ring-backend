"""
Интеграция: outbox-dispatcher (ревью 2026-10-06, BE-18).

- Сбой публикации одного события не срывает остаток пачки (регрессия;
  гипотеза ревью о MissingGreenlet после rollback проверена и не
  подтвердилась — тест фиксирует правильное поведение).
- У сообщения есть message_id = id события (дедупликация у потребителя).
- Пачка «застолблена» (locked_until): второй dispatcher её не берёт, пока
  первый публикует, — без двойной публикации.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from sqlalchemy import select, update

from app.models.outbox import OutboxEvent, OutboxStatus
from worker.handlers import outbox_handler


class _FakeExchange:
    def __init__(self, fail_on: set[str]) -> None:
        self.fail_on = fail_on
        self.published: list = []

    async def publish(self, message, routing_key):
        marker = message.body.decode()
        if any(f in marker for f in self.fail_on):
            raise RuntimeError("publish failed")
        self.published.append(message)


class _FakeChannel:
    def __init__(self, fail_on: set[str] | None = None) -> None:
        self.default_exchange = _FakeExchange(fail_on or set())


async def _events(db_session, *markers: str) -> list[OutboxEvent]:
    # Чужие pending-события (из других тестов/сидов) отодвигаем в будущее,
    # чтобы пачка состояла только из наших.
    await db_session.execute(
        update(OutboxEvent)
        .where(OutboxEvent.status == OutboxStatus.pending)
        .values(status=OutboxStatus.sent)
    )
    base = datetime.now(timezone.utc) - timedelta(minutes=10)
    rows = [
        OutboxEvent(
            routing_key="test_queue", payload={"marker": m},
            created_at=base + timedelta(seconds=i),
        )
        for i, m in enumerate(markers)
    ]
    db_session.add_all(rows)
    await db_session.commit()
    return rows


async def _status(db_session, ev_id):
    return (
        await db_session.execute(
            select(OutboxEvent.status, OutboxEvent.attempts).where(OutboxEvent.id == ev_id)
        )
    ).one()


async def test_one_failed_publish_does_not_break_the_batch(db_session, monkeypatch):
    monkeypatch.setattr(outbox_handler, "declare_workflow_queue", AsyncMock())
    rows = await _events(db_session, "first", "broken", "third")
    ids = [r.id for r in rows]
    channel = _FakeChannel(fail_on={"broken"})

    sent, failed = await outbox_handler.dispatch_once(db_session, channel)

    assert (sent, failed) == (2, 1)
    assert await _status(db_session, ids[0]) == (OutboxStatus.sent, 0)
    assert await _status(db_session, ids[1]) == (OutboxStatus.pending, 1)
    assert await _status(db_session, ids[2]) == (OutboxStatus.sent, 0)


async def test_message_id_is_event_id(db_session, monkeypatch):
    monkeypatch.setattr(outbox_handler, "declare_workflow_queue", AsyncMock())
    (row,) = await _events(db_session, "only")
    channel = _FakeChannel()
    await outbox_handler.dispatch_once(db_session, channel)
    (message,) = channel.default_exchange.published
    assert message.message_id == str(row.id)


async def test_claimed_batch_is_skipped_by_second_dispatcher(db_session, monkeypatch):
    monkeypatch.setattr(outbox_handler, "declare_workflow_queue", AsyncMock())
    (row,) = await _events(db_session, "claimed")
    # Первый dispatcher «застолбил» событие и ещё публикует.
    claimed = await outbox_handler.outbox_repo.claim_pending(db_session, limit=10)
    await db_session.commit()
    assert [e.id for e in claimed] == [row.id]

    channel = _FakeChannel()
    sent, failed = await outbox_handler.dispatch_once(db_session, channel)
    assert (sent, failed) == (0, 0)
    assert channel.default_exchange.published == []

