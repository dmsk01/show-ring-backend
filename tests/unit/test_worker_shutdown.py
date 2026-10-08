"""
Unit: корректное завершение воркера (ревью 2026-10-06, BE-20).

Раньше по SIGTERM соединение закрывалось сразу: обработчик, который в этот
момент работал, не успевал сделать ack — сообщение доставлялось повторно
(повторное письмо, повторная генерация документа). Теперь: сначала снимаем
consumer'ов (новые сообщения не приходят), ждём уже начатые обработчики
(с таймаутом), и только потом закрываем соединение.
"""

from __future__ import annotations

import asyncio

from worker import main as worker_main


async def test_inflight_tracker_waits_for_running_handler():
    tracker = worker_main.InFlight()
    release = asyncio.Event()

    async def handler(message):
        await release.wait()

    task = asyncio.create_task(tracker.wrap(handler)(object()))
    await asyncio.sleep(0)
    waiter = asyncio.create_task(tracker.wait_idle(timeout=5))
    await asyncio.sleep(0.01)
    assert not waiter.done()
    release.set()
    await task
    await asyncio.wait_for(waiter, 1)


async def test_serve_cancels_consumers_then_drains_then_closes():
    events: list[str] = []
    tracker = worker_main.InFlight()
    release = asyncio.Event()

    async def handler(message):
        await release.wait()
        events.append("handler_done")

    class _Queue:
        async def cancel(self, tag):
            events.append(f"cancel:{tag}")
            # Обработчик ещё идёт — завершится после снятия consumer'а.
            release.set()

    class _Conn:
        async def close(self):
            events.append("close")

    running = asyncio.create_task(tracker.wrap(handler)(object()))
    await asyncio.sleep(0)
    stop = asyncio.Event()
    stop.set()
    await worker_main._serve(
        _Conn(), "q", consumers=[(_Queue(), "ctag")], inflight=tracker, stop=stop
    )
    await running
    assert events == ["cancel:ctag", "handler_done", "close"]
