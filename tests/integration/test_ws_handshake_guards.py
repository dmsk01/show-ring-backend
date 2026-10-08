"""
Интеграция: защита WS-хендшейка (ревью 2026-10-06, BE-21).

- Origin: CSRF-middleware работает только для HTTP, а WS принимает
  httpOnly-куку из хендшейка — от cross-site WebSocket hijacking защищал
  лишь SameSite=Strict. Теперь чужой Origin → close(4403) до аутентификации.
- Первый кадр auth ждём ограниченное время: молчащий неаутентифицированный
  сокет больше не занимает слот --limit-concurrency бесконечно.
- У WS поддержки есть rate-limit подключений, как у уведомлений.
"""

from __future__ import annotations

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

import app.dependencies as deps
import app.routers.notifications as notif_router
import app.routers.support as support_router
from app.dependencies import WS_CLOSE_RATE_LIMITED
from app.main import app

_WS_PATHS = ("/ws/notifications", "/support/ws/00000000-0000-0000-0000-000000000001")


@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch):
    async def allow(websocket, *, limit, window):
        return True

    monkeypatch.setattr(notif_router, "ws_rate_limit", allow)
    monkeypatch.setattr(support_router, "ws_rate_limit", allow, raising=False)


@pytest.mark.parametrize("path", _WS_PATHS)
def test_foreign_origin_rejected(path):
    with TestClient(app) as c:
        with c.websocket_connect(path, headers={"origin": "https://evil.example"}) as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
    assert exc.value.code == deps.WS_CLOSE_FORBIDDEN_ORIGIN


@pytest.mark.parametrize("path", _WS_PATHS)
def test_silent_client_is_disconnected(monkeypatch, path):
    monkeypatch.setattr(deps, "WS_AUTH_TIMEOUT_SECONDS", 0.05)
    with TestClient(app) as c:
        with c.websocket_connect(path) as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
    assert exc.value.code == deps.WS_CLOSE_AUTH_TIMEOUT


def test_support_ws_is_rate_limited(monkeypatch):
    async def deny(websocket, *, limit, window):
        await websocket.close(code=WS_CLOSE_RATE_LIMITED)
        return False

    monkeypatch.setattr(support_router, "ws_rate_limit", deny, raising=False)
    with TestClient(app) as c:
        with c.websocket_connect(_WS_PATHS[1]) as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.send_json({"type": "auth", "token": "x"})
                ws.receive_json()
    assert exc.value.code == WS_CLOSE_RATE_LIMITED
