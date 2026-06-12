"""
Интеграция: полный auth-флоу через реальный HTTP-стек + БД + Redis.

Покрывает связку, которой не было в unit-тестах: middleware (sanitization,
proxy-headers, rate-limit) + роутер + сервис + репозиторий + Postgres.
"""

from __future__ import annotations

import uuid

PASSWORD = "secret123"  # 8–128 символов (validate_password)


def _email() -> str:
    return f"itest_{uuid.uuid4().hex[:10]}@example.com"


async def test_register_login_me_and_refresh_rotation(client):
    email = _email()

    # 1. Регистрация — единый ответ (без user enumeration).
    r = await client.post(
        "/auth/register", json={"email": email, "password": PASSWORD}
    )
    assert r.status_code == 200, r.text

    # 2. Логин — выдаёт пару токенов. Заголовок X-Token-Delivery: body —
    # «мобильный» режим: токены в теле ответа, без httpOnly-кук
    # (cookie-режим покрыт tests/integration/test_cookie_auth.py).
    r = await client.post(
        "/auth/login",
        json={"email": email, "password": PASSWORD},
        headers={"X-Token-Delivery": "body"},
    )
    assert r.status_code == 200, r.text
    tokens = r.json()
    access, refresh = tokens["access_token"], tokens["refresh_token"]

    # 3. Защищённый эндпоинт с токеном → 200 и наш email.
    r = await client.get(
        "/users/me", headers={"Authorization": f"Bearer {access}"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["email"] == email

    # 4. Без токена → 401.
    r = await client.get("/users/me")
    assert r.status_code == 401

    # 5. Неверный пароль → 401 (то же сообщение, что и «нет юзера»).
    r = await client.post(
        "/auth/login", json={"email": email, "password": "wrong-password"}
    )
    assert r.status_code == 401

    # 6. Ротация refresh: старый работает один раз, повторный — 401.
    r = await client.post(
        "/auth/refresh",
        json={"refresh_token": refresh},
        headers={"X-Token-Delivery": "body"},
    )
    assert r.status_code == 200, r.text
    new_refresh = r.json()["refresh_token"]
    assert new_refresh != refresh

    r = await client.post("/auth/refresh", json={"refresh_token": refresh})
    assert r.status_code == 401  # reuse отозванного refresh


async def test_login_is_rate_limited(client):
    # /auth/login: limit=5 / 60s. Redis flushed на старте теста, так что
    # счётчик чистый. После исчерпания лимита — 429 с Retry-After.
    email = _email()
    statuses = []
    for _ in range(8):
        r = await client.post(
            "/auth/login", json={"email": email, "password": "nope1234"}
        )
        statuses.append(r.status_code)
        if r.status_code == 429:
            assert "Retry-After" in r.headers
            break
    assert 429 in statuses, f"ожидали 429 в пределах 8 попыток, получили {statuses}"


async def test_register_user_enumeration_safe(client):
    # Повторная регистрация того же email отдаёт тот же 200-ответ, что и
    # новая, — наружу не светим, занят email или нет.
    email = _email()
    first = await client.post(
        "/auth/register", json={"email": email, "password": PASSWORD}
    )
    second = await client.post(
        "/auth/register", json={"email": email, "password": PASSWORD}
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()
