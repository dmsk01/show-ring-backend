"""
Интеграционные тесты feature flags (GET /feature-flags, PUT /feature-flags/{name})
и гейта require_flag.

Особенности харнесса:

- FlagService читает ГЛОБАЛ app.redis.redis_client (а не DI get_redis, как
  остальной стек). Поэтому monkeypatch'им глобал на изолированный test_redis
  (логическая db 15) — иначе сервис пошёл бы в dev-Redis или увидел None.

- flag_service — синглтон с in-memory кешем на ~2с. Чтобы кеш не протекал
  между тестами, НЕ используем синглтон: на каждый тест создаём свежий
  FlagService и подменяем им get_flag_service. require_flag и оба эндпойнта
  ходят через get_flag_service, поэтому одной подмены хватает обоим.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from app.main import app
from app.models.user import RoleEnum, UserRole
from app.services.feature_flags import (
    FeatureFlags,
    FlagService,
    get_flag_service,
    require_flag,
)

PASSWORD = "secret123"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _make_user(client) -> tuple[uuid.UUID, str]:
    """Регистрирует и логинит пользователя, возвращает (id, access_token)."""
    email = f"ff_{uuid.uuid4().hex[:10]}@example.com"
    await client.post(
        "/auth/register", json={"email": email, "password": PASSWORD}
    )
    r = await client.post(
        "/auth/login",
        json={"email": email, "password": PASSWORD},
        headers={"X-Token-Delivery": "body"},
    )
    access = r.json()["access_token"]
    me = await client.get("/users/me", headers=_auth(access))
    return uuid.UUID(me.json()["id"]), access


async def _make_admin(client, db_session) -> tuple[uuid.UUID, str]:
    uid, token = await _make_user(client)
    db_session.add(UserRole(user_id=uid, role=RoleEnum.admin))
    await db_session.commit()
    return uid, token


@pytest.fixture
def flag_svc(test_redis, monkeypatch):
    """Свежий FlagService на тест поверх изолированного Redis (db 15)."""
    # FlagService лезет в глобал redis_client — направляем на test_redis.
    monkeypatch.setattr("app.redis.redis_client", test_redis)
    svc = FlagService(FeatureFlags())
    app.dependency_overrides[get_flag_service] = lambda: svc
    yield svc
    app.dependency_overrides.pop(get_flag_service, None)


# ---------------------------------------------------------------------
# GET /feature-flags — публичный, для фронтенда
# ---------------------------------------------------------------------


async def test_list_flags_public_defaults(client, flag_svc):
    """Без авторизации; пустой Redis → все флаги по дефолту False."""
    r = await client.get("/feature-flags")
    assert r.status_code == 200
    body = r.json()
    # Известные флаги присутствуют и по умолчанию выключены.
    assert body["official_documents"] is False
    assert body["phone_otp_auth"] is False
    # Множество ключей ответа = поля FeatureFlags (ничего лишнего/недостающего).
    assert set(body) == set(FeatureFlags.model_fields)


async def test_list_reflects_value_set_in_redis(client, flag_svc, test_redis):
    """Значение из Redis перекрывает дефолт (read-path через MGET)."""
    await test_redis.set("ff:phone_otp_auth", "1")
    r = await client.get("/feature-flags")
    assert r.status_code == 200
    assert r.json()["phone_otp_auth"] is True


# ---------------------------------------------------------------------
# PUT /feature-flags/{name} — переключатель под админом
# ---------------------------------------------------------------------


async def test_toggle_requires_auth(client, flag_svc):
    """Аноним не может переключать флаги → 401."""
    r = await client.put("/feature-flags/official_documents", json={"enabled": True})
    assert r.status_code == 401


async def test_toggle_forbidden_for_plain_user(client, flag_svc):
    """Обычный пользователь (без роли admin) → 403."""
    _, token = await _make_user(client)
    r = await client.put(
        "/feature-flags/official_documents",
        json={"enabled": True},
        headers=_auth(token),
    )
    assert r.status_code == 403


async def test_admin_can_enable_flag(client, db_session, flag_svc, test_redis):
    """Админ включает флаг → 200, значение в Redis и в выдаче GET."""
    _, token = await _make_admin(client, db_session)

    r = await client.put(
        "/feature-flags/official_documents",
        json={"enabled": True},
        headers=_auth(token),
    )
    assert r.status_code == 200
    # Ответ — полный снапшот со свежим значением.
    assert r.json()["official_documents"] is True
    # Состояние реально записано в Redis ("1"/"0").
    assert await test_redis.get("ff:official_documents") == "1"
    # Публичный GET тоже видит включённый флаг.
    r2 = await client.get("/feature-flags")
    assert r2.json()["official_documents"] is True


async def test_admin_can_disable_flag(client, db_session, flag_svc, test_redis):
    """Выключение пишет '0' и снимает флаг в выдаче."""
    _, token = await _make_admin(client, db_session)
    await test_redis.set("ff:official_documents", "1")

    r = await client.put(
        "/feature-flags/official_documents",
        json={"enabled": False},
        headers=_auth(token),
    )
    assert r.status_code == 200
    assert r.json()["official_documents"] is False
    assert await test_redis.get("ff:official_documents") == "0"


async def test_toggle_unknown_flag_404(client, db_session, flag_svc):
    """Неизвестное имя нельзя писать в Redis → 404 (не плодим мусорные ключи)."""
    _, token = await _make_admin(client, db_session)
    r = await client.put(
        "/feature-flags/does_not_exist",
        json={"enabled": True},
        headers=_auth(token),
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------
# require_flag — гейт роутов (404, а не 403, когда фича выключена)
# ---------------------------------------------------------------------


async def test_require_flag_blocks_when_disabled(flag_svc):
    """Выключенный флаг → HTTPException 404 (фича выглядит несуществующей)."""
    dep = require_flag("official_documents")
    with pytest.raises(HTTPException) as exc:
        await dep(service=flag_svc)
    assert exc.value.status_code == 404


async def test_require_flag_passes_when_enabled(flag_svc):
    """Включённый флаг пропускает запрос дальше (зависимость не кидает)."""
    await flag_svc.set("official_documents", True)
    dep = require_flag("official_documents")
    # Не должно поднять исключение; зависимость ничего не возвращает.
    assert await dep(service=flag_svc) is None
