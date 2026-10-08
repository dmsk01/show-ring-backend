"""
Интеграция: необработанные ошибки (ревью 2026-10-06, BE-22).

- Ответ 500 проходит через CORS и RequestId: раньше обработчик Exception
  жил в ServerErrorMiddleware — СНАРУЖИ всех middleware, браузер видел
  CORS-ошибку вместо 500, а X-Request-ID в ответе не было.
- request_id попадает в запись лога с traceback — по id из ответа можно
  найти стек.
- OSError (сеть до S3/SMTP, файловая система) не выдаётся за «БД недоступна».
"""

from __future__ import annotations

import logging

import pytest
from fastapi import APIRouter

from app.config import settings
from app.main import app

_router = APIRouter()


@_router.get("/__test/boom")
async def _boom():
    raise RuntimeError("unexpected")


@_router.get("/__test/oserror")
async def _oserror():
    raise ConnectionRefusedError("s3 down")


@pytest.fixture
def boom_routes():
    before = list(app.router.routes)
    app.include_router(_router)
    try:
        yield
    finally:
        app.router.routes[:] = before


async def test_500_has_cors_and_request_id(client, boom_routes, caplog):
    if not settings.cors_allow_origins:
        pytest.skip("CORS_ALLOW_ORIGINS не задан — CORS не подключён")
    origin = settings.cors_allow_origins[0]
    with caplog.at_level(logging.ERROR):
        r = await client.get("/__test/boom", headers={"Origin": origin})
    assert r.status_code == 500
    assert r.headers.get("access-control-allow-origin") == origin
    request_id = r.headers.get("x-request-id")
    assert request_id and r.json()["request_id"] == request_id
    error_records = [rec for rec in caplog.records if rec.exc_info]
    assert error_records, "traceback должен быть в логе"
    assert all(getattr(rec, "request_id", None) == request_id for rec in error_records)
    assert len(error_records) == 1, "traceback логируется один раз"


async def test_oserror_is_not_reported_as_database_down(client, boom_routes):
    r = await client.get("/__test/oserror")
    assert r.status_code == 503
    assert "Database" not in r.json()["detail"]
