"""
Интеграция: жизненный цикл приложения и cron-блокировки (ревью 2026-10-06, BE-31).

- Shutdown: сначала планировщик (его задачи пользуются БД/Redis/Rabbit),
  потом Rabbit, Redis и последним — пул БД. Раньше пул закрывался первым,
  а работающая cron-задача получала закрытый engine.
- Scheduler-lock продлевается, пока задача работает: TTL 300 с меньше
  длительности retention/архивации на больших таблицах — после истечения
  вторая реплика запускала ту же задачу параллельно.
"""

from __future__ import annotations

import asyncio

from app import main as app_main
from app.services import scheduler


async def test_shutdown_order(monkeypatch):
    calls: list[str] = []

    async def rec(name):
        calls.append(name)

    class _Engine:
        async def dispose(self):
            calls.append("engine")

    monkeypatch.setattr(app_main, "stop_scheduler", lambda: rec("scheduler"))
    monkeypatch.setattr(app_main.rabbit_service, "close", lambda: rec("rabbit"))
    monkeypatch.setattr(app_main, "close_redis", lambda: rec("redis"))
    monkeypatch.setattr(app_main, "engine", _Engine())

    await app_main.shutdown_resources()

    assert calls == ["scheduler", "rabbit", "redis", "engine"]


async def test_scheduler_lock_is_extended_while_job_runs(test_redis, monkeypatch):
    monkeypatch.setattr("app.redis.redis_client", test_redis)
    key = "scheduler:lock:long_job"
    async with scheduler._scheduler_lock("long_job", ttl_seconds=1) as acquired:
        assert acquired
        await asyncio.sleep(1.6)  # дольше TTL
        assert await test_redis.exists(key), "lock истёк во время работы задачи"
    assert not await test_redis.exists(key)
