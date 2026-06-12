"""
Этап 16 — realtime-уведомления.

Два блока:

1. WS-эндпоинт /ws/notifications (через синхронный TestClient — httpx
   AsyncClient не умеет WebSocket). authenticate_ws и notif_ws_manager
   подменяются, чтобы проверить именно ПРОВОДКУ эндпоинта (accept →
   auth-контракт → auth_ok → регистрация сокета → проброс пуша →
   disconnect) детерминированно, без живых БД/Redis. Сам механизм
   Redis→сокет покрыт в tests/unit/test_ws_manager.py.

2. Фильтр канала GET /notifications?channel=in_app (через реальный
   async-харнесс): in_app-строки не смешиваются с журналом email.
   Требует накатанной миграции a7d4e9c1f3b8 (значение in_app в PG-enum).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

import app.routers.notifications as notif_router
from app.dependencies import WS_CLOSE_RATE_LIMITED
from app.main import app
from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationStatus,
)

PASSWORD = "secret123"


# ---------------------------------------------------------------------
# 1. WS-эндпоинт
# ---------------------------------------------------------------------


def test_ws_handshake_and_push(monkeypatch):
    """auth → auth_ok → пуш доезжает до клиента; disconnect вызывается."""
    fake_user = SimpleNamespace(id=uuid.uuid4(), is_active=True)

    async def fake_auth(db, token, websocket=None):
        return fake_user if token == "good" else None

    disconnected: list = []

    async def fake_connect(key, ws):
        # Симулируем realtime-push сразу при регистрации сокета —
        # как будто из Redis прилетело уведомление.
        assert key == fake_user.id
        await ws.send_json({"type": "notification", "payload": {"id": "x"}})

    async def fake_disconnect(key, ws):
        disconnected.append(key)

    monkeypatch.setattr(notif_router, "authenticate_ws", fake_auth)
    monkeypatch.setattr(notif_router.notif_ws_manager, "connect", fake_connect)
    monkeypatch.setattr(
        notif_router.notif_ws_manager, "disconnect", fake_disconnect
    )

    with TestClient(app) as c:
        with c.websocket_connect("/ws/notifications") as ws:
            ws.send_json({"type": "auth", "token": "good"})
            hello = ws.receive_json()
            assert hello["type"] == "auth_ok"
            assert hello["payload"]["user_id"] == str(fake_user.id)
            push = ws.receive_json()
            assert push == {"type": "notification", "payload": {"id": "x"}}

    # finally-блок эндпоинта снял регистрацию сокета.
    assert disconnected == [fake_user.id]


def test_ws_rejects_bad_token(monkeypatch):
    """Невалидный токен → error-кадр и закрытие, регистрации сокета нет."""
    async def fake_auth(db, token, websocket=None):
        return None

    connected: list = []

    async def fake_connect(key, ws):  # pragma: no cover — не должен вызваться
        connected.append(key)

    monkeypatch.setattr(notif_router, "authenticate_ws", fake_auth)
    monkeypatch.setattr(notif_router.notif_ws_manager, "connect", fake_connect)

    with TestClient(app) as c:
        with c.websocket_connect("/ws/notifications") as ws:
            ws.send_json({"type": "auth", "token": "bad"})
            data = ws.receive_json()
            assert data["type"] == "error"
            assert data["payload"]["code"] == "invalid_token"

    assert connected == []


def test_ws_requires_auth_frame_first(monkeypatch):
    """Первый кадр не auth → error + close."""
    async def fake_auth(db, token, websocket=None):  # pragma: no cover
        raise AssertionError("authenticate_ws не должен вызываться")

    monkeypatch.setattr(notif_router, "authenticate_ws", fake_auth)

    with TestClient(app) as c:
        with c.websocket_connect("/ws/notifications") as ws:
            ws.send_json({"type": "message", "body": "hi"})
            data = ws.receive_json()
            assert data["type"] == "error"
            assert data["payload"]["code"] == "auth_required"


def test_ws_rate_limited(monkeypatch):
    """Превышение rate-limit → сокет закрыт с 4429, auth не запускается."""
    async def fake_rl(websocket, *, limit, window):
        await websocket.close(code=WS_CLOSE_RATE_LIMITED)
        return False

    async def fake_auth(db, token, websocket=None):  # pragma: no cover — не должен вызваться
        raise AssertionError("authenticate_ws не должен вызываться при rate-limit")

    monkeypatch.setattr(notif_router, "ws_rate_limit", fake_rl)
    monkeypatch.setattr(notif_router, "authenticate_ws", fake_auth)

    with TestClient(app) as c:
        with c.websocket_connect("/ws/notifications") as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.send_json({"type": "auth", "token": "good"})
                ws.receive_json()
    assert exc.value.code == WS_CLOSE_RATE_LIMITED


# ---------------------------------------------------------------------
# 2. Фильтр канала GET /notifications?channel=in_app
# ---------------------------------------------------------------------


async def _make_user(client) -> tuple[uuid.UUID, str]:
    email = f"itest_{uuid.uuid4().hex[:10]}@example.com"
    await client.post(
        "/auth/register", json={"email": email, "password": PASSWORD}
    )
    r = await client.post(
        "/auth/login",
        json={"email": email, "password": PASSWORD},
        headers={"X-Token-Delivery": "body"},
    )
    access = r.json()["access_token"]
    me = await client.get(
        "/users/me", headers={"Authorization": f"Bearer {access}"}
    )
    return uuid.UUID(me.json()["id"]), access


async def _add_notification(
    db, user_id: uuid.UUID, channel: NotificationChannel, subject: str
) -> uuid.UUID:
    n = Notification(
        user_id=user_id,
        event_type="dog.title_earned",
        channel=channel,
        subject=subject,
        status=NotificationStatus.sent,
    )
    db.add(n)
    await db.commit()
    return n.id


async def test_channel_filter(client, db_session):
    """?channel=in_app отдаёт только in_app; без фильтра — все каналы."""
    uid, token = await _make_user(client)
    auth = {"Authorization": f"Bearer {token}"}

    email_id = await _add_notification(
        db_session, uid, NotificationChannel.email, "Email-журнал"
    )
    in_app_id = await _add_notification(
        db_session, uid, NotificationChannel.in_app, "In-app колокольчик"
    )

    # Фильтр in_app: только in_app-строка.
    r = await client.get("/notifications?channel=in_app", headers=auth)
    assert r.status_code == 200, r.text
    ids = {n["id"] for n in r.json()}
    assert str(in_app_id) in ids
    assert str(email_id) not in ids
    assert all(n["channel"] == "in_app" for n in r.json())

    # Фильтр email: только email-строка.
    r = await client.get("/notifications?channel=email", headers=auth)
    assert {n["id"] for n in r.json()} >= {str(email_id)}
    assert str(in_app_id) not in {n["id"] for n in r.json()}

    # Без фильтра — обе.
    r = await client.get("/notifications", headers=auth)
    ids = {n["id"] for n in r.json()}
    assert {str(email_id), str(in_app_id)} <= ids


async def test_channel_filter_rejects_unknown(client):
    """Неизвестный канал → 422 (FastAPI валидирует enum)."""
    _uid, token = await _make_user(client)
    r = await client.get(
        "/notifications?channel=telegram",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422
