"""
Unit: Sanitization и Idempotency как чистые ASGI-middleware (ревью
2026-10-06, BE-28).

Раньше обе были BaseHTTPMiddleware и подменяли тело через приватные
request._body/_receive (однажды handler из-за этого получал НЕочищенное
тело), а idempotency буферизовала и пересобирала каждый ответ.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from httpx import ASGITransport, AsyncClient

from app.middleware import idempotency as idem
from app.middleware.asgi_body import replay_receive
from app.middleware.idempotency import IdempotencyMiddleware
from app.middleware.sanitization import SanitizationMiddleware
from tests.unit.test_redis_module_binding import _FakeRedis


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


@pytest.fixture
def fake_redis(monkeypatch) -> _FakeRedis:
    fake = _FakeRedis()
    monkeypatch.setattr("app.redis.redis_client", fake)
    return fake


# --- Sanitization ---------------------------------------------------------


def _sanitizing_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(SanitizationMiddleware)

    @app.post("/echo")
    async def echo(request: Request):
        raw = await request.body()
        return {
            "body": json.loads(raw),
            "content_length": request.headers.get("content-length"),
            "actual_length": len(raw),
        }

    return app


async def test_handler_receives_sanitized_body_with_matching_length():
    async with _client(_sanitizing_app()) as c:
        r = await c.post("/echo", json={"name": "<b>Рекс</b>", "password": "a<b>c"})
    data = r.json()
    assert data["body"] == {"name": "Рекс", "password": "a<b>c"}
    assert int(data["content_length"]) == data["actual_length"]


async def test_invalid_json_rejected_before_handler():
    async with _client(_sanitizing_app()) as c:
        r = await c.post(
            "/echo", content=b"{broken", headers={"Content-Type": "application/json"}
        )
    assert r.status_code == 400
    assert r.json() == {"detail": "Invalid JSON"}


async def test_non_json_body_passes_untouched():
    app = FastAPI()
    app.add_middleware(SanitizationMiddleware)

    @app.post("/raw")
    async def raw(request: Request):
        return {"body": (await request.body()).decode()}

    async with _client(app) as c:
        r = await c.post(
            "/raw", content=b"<b>x</b>", headers={"Content-Type": "text/plain"}
        )
    assert r.json() == {"body": "<b>x</b>"}


async def test_replay_receive_hands_over_to_client_stream():
    # После подменённого тела приложение должно видеть сообщения клиента
    # (http.disconnect) — на них держатся StreamingResponse и long-poll.
    async def client_receive():
        return {"type": "http.disconnect"}

    receive = replay_receive(b"body", client_receive)
    assert (await receive())["body"] == b"body"
    assert (await receive())["type"] == "http.disconnect"


# --- Idempotency ----------------------------------------------------------


def _idem_app(calls: list[str]) -> FastAPI:
    app = FastAPI()
    app.add_middleware(IdempotencyMiddleware)

    @app.post("/stream")
    async def stream():
        calls.append("stream")

        async def gen():
            yield b'{"part": 1'
            yield b', "done": true}'

        return StreamingResponse(gen(), media_type="application/json")

    @app.post("/big")
    async def big():
        calls.append("big")
        return {"data": "x" * (idem._MAX_CACHED_BODY_BYTES + 10)}

    return app


async def test_streaming_response_passes_through_and_is_cached(fake_redis):
    calls: list[str] = []
    headers = {"Idempotency-Key": "s1"}
    async with _client(_idem_app(calls)) as c:
        first = await c.post("/stream", headers=headers, json={})
        second = await c.post("/stream", headers=headers, json={})
    assert first.json() == second.json() == {"part": 1, "done": True}
    assert second.headers["content-type"] == "application/json"
    assert calls == ["stream"]


async def test_oversized_response_delivered_but_not_cached(fake_redis):
    calls: list[str] = []
    headers = {"Idempotency-Key": "b1"}
    async with _client(_idem_app(calls)) as c:
        first = await c.post("/big", headers=headers, json={})
        second = await c.post("/big", headers=headers, json={})
    assert len(first.json()["data"]) == idem._MAX_CACHED_BODY_BYTES + 10
    assert second.status_code == 200
    # Кэша нет — второй запрос выполнился заново (lock к этому моменту снят).
    assert calls == ["big", "big"]


async def test_legacy_snapshot_with_header_dict_is_replayed(fake_redis):
    # Снимки, записанные до BE-28 (headers — словарь), живут в Redis до суток.
    calls: list[str] = []
    body = b"{}"
    import hashlib

    key = idem._cache_key(
        "POST", "/stream", "legacy", hashlib.sha256(body).hexdigest(), "ip:127.0.0.1"
    )
    fake_redis.store[key] = json.dumps({
        "body": '{"old": true}',
        "encoding": "utf-8",
        "status": 201,
        "headers": {"content-type": "application/json", "x-extra": "1"},
        "media_type": "application/json",
    })
    async with _client(_idem_app(calls)) as c:
        r = await c.post(
            "/stream",
            content=body,
            headers={"Idempotency-Key": "legacy", "Content-Type": "application/json"},
        )
    assert r.status_code == 201
    assert r.json() == {"old": True}
    assert r.headers["x-extra"] == "1"
    assert calls == []
