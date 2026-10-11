"""
Аккумулятор рекламных событий: сообщение из очереди подтверждается только
после записи его батча в БД (ревью 2026-10-06, BE-06).

Раньше `on_ad_event_message` клал событие в буфер и сразу возвращался —
обёртка `message.process()` подтверждала сообщение до INSERT. При падении
воркера буфер (до 50 событий или 2 с) терялся, а RabbitMQ считал их
доставленными: биллинг недосчитывал показы.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from worker.handlers import ad_handler


@asynccontextmanager
async def _fake_session():
    yield object()


def _event(i: int) -> dict:
    return {"banner_id": f"00000000-0000-0000-0000-{i:012d}", "event_type": "impression"}


@pytest.fixture
def accumulator(monkeypatch):
    acc = ad_handler.BatchAccumulator(_fake_session)
    monkeypatch.setattr(ad_handler, "_accumulator", acc)
    return acc


async def test_message_is_not_done_until_batch_is_written(accumulator, monkeypatch):
    written: list[list[dict]] = []
    gate = asyncio.Event()

    async def slow_process(db, batch):
        await gate.wait()
        written.append(batch)

    monkeypatch.setattr(ad_handler, "process_batch", slow_process)
    monkeypatch.setattr(ad_handler, "BATCH_SIZE", 2)

    first = asyncio.create_task(ad_handler.on_ad_event_message(json.dumps(_event(1))))
    await asyncio.sleep(0)
    # Батч ещё не набран и не записан — обработчик не должен завершиться
    # (иначе message.process() подтвердит сообщение раньше времени).
    assert not first.done()

    second = asyncio.create_task(ad_handler.on_ad_event_message(json.dumps(_event(2))))
    await asyncio.sleep(0)
    assert not first.done() and not second.done()

    gate.set()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=1)
    assert len(written) == 1 and len(written[0]) == 2


async def test_failed_write_keeps_message_pending_and_retries(accumulator, monkeypatch):
    calls = 0

    async def flaky_process(db, batch):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("db down")

    monkeypatch.setattr(ad_handler, "process_batch", flaky_process)
    monkeypatch.setattr(ad_handler, "BATCH_SIZE", 1)

    handler = asyncio.create_task(ad_handler.on_ad_event_message(json.dumps(_event(1))))
    await asyncio.sleep(0.01)
    # Запись упала: сообщение не подтверждено, событие вернулось в буфер.
    assert not handler.done()
    assert len(accumulator._buffer) == 1

    # Следующая попытка (периодический flush) записывает и отпускает сообщение.
    await accumulator.stop()
    await asyncio.wait_for(handler, timeout=1)
    assert calls == 2


async def test_dropped_on_overflow_fails_the_message(accumulator, monkeypatch):
    async def failing_process(db, batch):
        raise RuntimeError("db down")

    monkeypatch.setattr(ad_handler, "process_batch", failing_process)
    monkeypatch.setattr(ad_handler, "BATCH_SIZE", 1)
    monkeypatch.setattr(ad_handler, "_MAX_BUFFER_SIZE", 1)

    old = asyncio.create_task(ad_handler.on_ad_event_message(json.dumps(_event(1))))
    await asyncio.sleep(0.01)
    new = asyncio.create_task(ad_handler.on_ad_event_message(json.dumps(_event(2))))
    await asyncio.sleep(0.01)

    # Самое старое событие вытеснено из буфера — его сообщение завершается
    # ошибкой (nack → DLQ), а не «успехом» без записи.
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(old, timeout=1)
    assert not new.done()
    new.cancel()
