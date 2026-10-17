"""
Unit: приём отчётов о нарушениях CSP (/csp-report).

В лог уходят только директива, источник заблокированного ресурса без
пути и путь страницы без query — токены из ссылок в письмах не должны
оседать в логах.
"""

from __future__ import annotations

import json
import logging

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.routers import csp_report
from app.services import security_metrics


@pytest.fixture
def recorded(monkeypatch) -> list[tuple[str, int]]:
    calls: list[tuple[str, int]] = []

    async def _record(event, n=1, **_kwargs):
        calls.append((event, n))

    monkeypatch.setattr(security_metrics, "record", _record)
    monkeypatch.setattr("app.redis.redis_client", None)
    return calls


def _client() -> AsyncClient:
    app = FastAPI()
    app.include_router(csp_report.router)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


LEGACY = {
    "csp-report": {
        "document-uri": "https://site.ru/auth/verify-email?token=SECRET#x",
        "violated-directive": "script-src-elem",
        "effective-directive": "script-src-elem",
        "blocked-uri": "https://evil.example.com/x.js?session=SECRET",
        "disposition": "enforce",
    }
}

REPORTING_API = [
    {
        "type": "csp-violation",
        "url": "https://site.ru/dogs/1",
        "body": {
            "documentURL": "https://site.ru/dogs/1?q=1",
            "effectiveDirective": "img-src",
            "blockedURL": "https://cdn.example.org/a.png",
            "disposition": "report",
        },
    },
    {"type": "deprecation", "body": {}},
]


async def test_legacy_report_logged_without_secrets(recorded, caplog):
    caplog.set_level(logging.WARNING, logger="app.security")
    async with _client() as c:
        r = await c.post(
            "/csp-report",
            content=json.dumps(LEGACY),
            headers={"Content-Type": "application/csp-report"},
        )
    assert r.status_code == 204
    text = caplog.text
    assert "directive=script-src-elem" in text
    assert "blocked=https://evil.example.com " in text
    assert "page=/auth/verify-email " in text
    assert "SECRET" not in text
    assert recorded == [(security_metrics.CSP_VIOLATION, 1)]


async def test_reporting_api_format(recorded, caplog):
    caplog.set_level(logging.WARNING, logger="app.security")
    async with _client() as c:
        r = await c.post(
            "/csp-report",
            content=json.dumps(REPORTING_API),
            headers={"Content-Type": "application/reports+json"},
        )
    assert r.status_code == 204
    assert "directive=img-src blocked=https://cdn.example.org page=/dogs/1" in caplog.text
    # Отчёт другого типа (deprecation) не считается нарушением CSP.
    assert recorded == [(security_metrics.CSP_VIOLATION, 1)]


@pytest.mark.parametrize("blocked,expected", [
    ("inline", "inline"),
    ("eval", "eval"),
    ("data:image/png;base64,AAAA", "data"),
    ("", "-"),
])
def test_origin_keywords(blocked, expected):
    assert csp_report._origin(blocked) == expected


async def test_garbage_is_accepted_silently(recorded):
    async with _client() as c:
        r = await c.post("/csp-report", content=b"not json")
    assert r.status_code == 204
    assert recorded == []


async def test_oversized_report_rejected(recorded):
    async with _client() as c:
        r = await c.post("/csp-report", content=b"x" * (17 * 1024))
    assert r.status_code == 413
    assert recorded == []
