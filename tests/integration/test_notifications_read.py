"""
Интеграция: отметка уведомления прочитанным (PATCH /notifications/{id}/read).

Проверяет:
- is_read/read_at в ответе (изначально false/null);
- PATCH помечает прочитанным (is_read=true, read_at проставлен);
- идемпотентность повторного PATCH;
- IDOR: чужое уведомление → 404; без токена → 401.
"""

from __future__ import annotations

import uuid

import app.config
from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationStatus,
)

PASSWORD = "secret123"


async def _make_user(client) -> tuple[uuid.UUID, str]:
    email = f"itest_{uuid.uuid4().hex[:10]}@example.com"
    await client.post(
        "/auth/register", json={"email": email, "password": PASSWORD}
    )
    r = await client.post(
        "/auth/login", json={"email": email, "password": PASSWORD}
    )
    access = r.json()["access_token"]
    me = await client.get(
        "/users/me", headers={"Authorization": f"Bearer {access}"}
    )
    return uuid.UUID(me.json()["id"]), access


async def _make_notification(db_session, user_id: uuid.UUID) -> uuid.UUID:
    n = Notification(
        user_id=user_id,
        event_type="dog.title_earned",
        channel=NotificationChannel.email,
        subject="Ваша собака получила титул",
        status=NotificationStatus.sent,
    )
    db_session.add(n)
    await db_session.commit()
    return n.id


async def test_mark_notification_read_flow(client, db_session):
    uid, token = await _make_user(client)
    nid = await _make_notification(db_session, uid)
    auth = {"Authorization": f"Bearer {token}"}

    # Изначально непрочитано.
    r = await client.get("/notifications", headers=auth)
    assert r.status_code == 200
    item = next(n for n in r.json() if n["id"] == str(nid))
    assert item["is_read"] is False
    assert item["read_at"] is None

    # Отмечаем прочитанным.
    r = await client.patch(f"/notifications/{nid}/read", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["is_read"] is True
    assert body["read_at"] is not None

    # Идемпотентность: повторный PATCH — снова 200, всё ещё прочитано.
    r2 = await client.patch(f"/notifications/{nid}/read", headers=auth)
    assert r2.status_code == 200
    assert r2.json()["is_read"] is True
    # read_at не перетёрся.
    assert r2.json()["read_at"] == body["read_at"]

    # В списке теперь тоже прочитано.
    r = await client.get("/notifications", headers=auth)
    item = next(n for n in r.json() if n["id"] == str(nid))
    assert item["is_read"] is True


async def test_mark_read_idor_and_auth(client, db_session):
    owner_id, _owner_token = await _make_user(client)
    _other_id, other_token = await _make_user(client)
    nid = await _make_notification(db_session, owner_id)

    # Чужой пользователь не видит/не трогает чужое уведомление → 404.
    r = await client.patch(
        f"/notifications/{nid}/read",
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert r.status_code == 404

    # Без токена → 401.
    r = await client.patch(f"/notifications/{nid}/read")
    assert r.status_code == 401

    # Несуществующее уведомление у владельца → 404.
    r = await client.patch(
        f"/notifications/{uuid.uuid4()}/read",
        headers={"Authorization": f"Bearer {_owner_token}"},
    )
    assert r.status_code == 404


async def test_unread_count_and_read_all(client, db_session):
    uid, token = await _make_user(client)
    auth = {"Authorization": f"Bearer {token}"}
    for _ in range(3):
        await _make_notification(db_session, uid)

    r = await client.get("/notifications/unread-count", headers=auth)
    assert r.status_code == 200
    assert r.json()["unread"] == 3

    # Отметить все → marked=3, счётчик обнуляется.
    r = await client.patch("/notifications/read-all", headers=auth)
    assert r.status_code == 200
    assert r.json()["marked"] == 3

    r = await client.get("/notifications/unread-count", headers=auth)
    assert r.json()["unread"] == 0

    # Повторно нечего отмечать.
    r = await client.patch("/notifications/read-all", headers=auth)
    assert r.json()["marked"] == 0


async def test_seed_endpoint_requires_debug(client, monkeypatch):
    _uid, token = await _make_user(client)
    auth = {"Authorization": f"Bearer {token}"}

    # DEBUG выключен (дефолт в тест-окружении) → ручки «нет» (404).
    monkeypatch.setattr(app.config.settings, "debug", False)
    r = await client.post("/notifications/_dev/seed?count=3", headers=auth)
    assert r.status_code == 404

    # DEBUG включён → набивает непрочитанные моки.
    monkeypatch.setattr(app.config.settings, "debug", True)
    r = await client.post("/notifications/_dev/seed?count=4", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body) == 4
    assert all(item["is_read"] is False for item in body)

    r = await client.get("/notifications/unread-count", headers=auth)
    assert r.json()["unread"] == 4
