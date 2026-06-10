"""
Регрессия на класс бага «stale-binding redis_client» (review 2026-06-10).

`from app.redis import redis_client` копирует значение (None) на момент
импорта модуля — init_redis() пере-присваивает app.redis.redis_client уже
ПОСЛЕ, и стейл-имя остаётся None навсегда. Найдено и исправлено уже в
третьем и четвёртом месте (ws_manager → idempotency → ad), поэтому здесь:

1. функциональные тесты: подменяем app.redis.redis_client фейком и
   проверяем, что idempotency-кэш и ad-дедупликация реально работают
   (на стейл-импортной версии фейк до кода бы не дошёл);
2. структурный тест-запрет: value-импорт redis_client из app.redis
   запрещён во всём app/ и worker/ — только модульный импорт
   (`from app import redis as redis_state`).
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.middleware.idempotency import IdempotencyMiddleware
from app.models.ad import AdEventType
from app.services import ad as ad_svc


# ---------------------------------------------------------------------
# Фейк Redis: ровно те операции, что нужны idempotency и дедупликации
# ---------------------------------------------------------------------


class _FakeRedis:
    """In-memory get/set/delete с поддержкой NX (без TTL-истечения)."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key, value, nx: bool = False, ex: int | None = None):
        if nx and key in self.store:
            return None  # redis-py: NX на существующем ключе → None
        self.store[key] = value
        return True

    async def delete(self, key: str) -> int:
        return 1 if self.store.pop(key, None) is not None else 0


@pytest.fixture
def fake_redis(monkeypatch) -> _FakeRedis:
    fake = _FakeRedis()
    # Патчим атрибут МОДУЛЯ app.redis — потребители обязаны читать его
    # через модуль; стейл-импортная версия этот патч не увидит.
    monkeypatch.setattr("app.redis.redis_client", fake)
    return fake


# ---------------------------------------------------------------------
# IdempotencyMiddleware
# ---------------------------------------------------------------------


def _make_app() -> tuple[FastAPI, dict[str, int]]:
    app = FastAPI()
    app.add_middleware(IdempotencyMiddleware)
    calls = {"n": 0}

    @app.post("/op")
    async def op():
        calls["n"] += 1
        return {"call": calls["n"]}

    return app, calls


async def test_idempotency_replays_cached_response(fake_redis):
    """Повтор POST с тем же Idempotency-Key отдаёт кэш, handler не дёргается."""
    app, calls = _make_app()
    transport = ASGITransport(app=app)
    headers = {"Idempotency-Key": "key-1"}
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        r1 = await client.post("/op", headers=headers)
        r2 = await client.post("/op", headers=headers)

    assert r1.status_code == 200
    assert r2.status_code == 200
    # Главная проверка: операция выполнена ОДИН раз, второй ответ — из кэша.
    assert calls["n"] == 1
    assert r2.json() == r1.json()


async def test_idempotency_in_flight_lock_blocks_duplicate(fake_redis):
    """Если lock уже стоит (запрос «в полёте»), дубль получает 409."""
    app, calls = _make_app()
    # Эмулируем «первый запрос ещё обрабатывается»: SETNX на lock-ключ
    # не проходит — middleware обязан ответить 409, не зовя handler.
    original_set = fake_redis.set

    async def deny_lock(key, value, nx: bool = False, ex: int | None = None):
        if nx and key.startswith("idem:lock:"):
            return None
        return await original_set(key, value, nx=nx, ex=ex)

    fake_redis.set = deny_lock  # type: ignore[method-assign]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.post("/op", headers={"Idempotency-Key": "key-2"})

    assert r.status_code == 409
    assert calls["n"] == 0


async def test_idempotency_passthrough_without_key(fake_redis):
    """Без заголовка каждый запрос выполняется заново (кэша нет)."""
    app, calls = _make_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        await client.post("/op")
        await client.post("/op")
    assert calls["n"] == 2
    assert fake_redis.store == {}


# ---------------------------------------------------------------------
# Ad: дедупликация событий
# ---------------------------------------------------------------------


async def test_ad_duplicate_event_detected_within_window(fake_redis):
    """Второе идентичное событие в окне помечается дублем (фрод-фильтр)."""
    banner_id = uuid.uuid4()
    first = await ad_svc._is_duplicate(
        banner_id, AdEventType.click, "1.2.3.4", "ua-hash"
    )
    second = await ad_svc._is_duplicate(
        banner_id, AdEventType.click, "1.2.3.4", "ua-hash"
    )
    assert first is False
    assert second is True


async def test_ad_dedup_distinct_clients_not_duplicates(fake_redis):
    """Разные ip/ua не считаются дублями друг друга."""
    banner_id = uuid.uuid4()
    a = await ad_svc._is_duplicate(banner_id, AdEventType.click, "1.1.1.1", "ua-a")
    b = await ad_svc._is_duplicate(banner_id, AdEventType.click, "2.2.2.2", "ua-b")
    assert a is False
    assert b is False


# ---------------------------------------------------------------------
# Структурный запрет value-импорта
# ---------------------------------------------------------------------


def test_no_value_imports_of_redis_client():
    """`from app.redis import redis_client` запрещён — только модульный импорт.

    Значение копируется до init_redis() и остаётся None навсегда. Это уже
    четырежды выстреливало (ws_manager, idempotency, ad) — фиксируем тестом.
    """
    root = Path(__file__).resolve().parents[2]
    # Построчно и без комментариев: в health.py/ws_manager.py этот импорт
    # упоминается в предупреждающих комментариях — это не нарушение.
    pattern = re.compile(r"^\s*from\s+app\.redis\s+import\s+.*\bredis_client\b")
    offenders: list[str] = []
    for top in ("app", "worker"):
        for path in (root / top).rglob("*.py"):
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.lstrip().startswith("#"):
                    continue
                if pattern.match(line):
                    offenders.append(str(path.relative_to(root)))
                    break
    assert offenders == [], (
        "value-импорт redis_client найден (используйте "
        f"`from app import redis as redis_state`): {offenders}"
    )
