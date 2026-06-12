"""
Интеграция cookie-режима auth (дефолт для веба, 2026-06).

Логин без заголовка X-Token-Delivery кладёт оба токена в httpOnly-куки,
в теле — null. Куки шлём вручную через заголовок Cookie: в .env обычно
DEBUG=false → куки Secure, а httpx-jar не возвращает Secure-куки по
http://test — ручная отправка делает тесты независимыми от настройки.
"""

from __future__ import annotations

import uuid

PASSWORD = "secret123"


def _email() -> str:
    return f"cookie_{uuid.uuid4().hex[:10]}@example.com"


def _set_cookies(r) -> dict[str, str]:
    """Имя куки → значение из всех Set-Cookie заголовков ответа."""
    out: dict[str, str] = {}
    for header in r.headers.get_list("set-cookie"):
        first = header.split(";", 1)[0]
        name, _, value = first.partition("=")
        out[name.strip()] = value.strip('"')
    return out


async def _register_and_login(client, *, headers: dict | None = None):
    email = _email()
    r = await client.post(
        "/auth/register", json={"email": email, "password": PASSWORD}
    )
    assert r.status_code == 200, r.text
    r = await client.post(
        "/auth/login",
        json={"email": email, "password": PASSWORD},
        headers=headers or {},
    )
    assert r.status_code == 200, r.text
    return r


async def test_login_default_sets_httponly_cookies(client):
    r = await _register_and_login(client)

    # Тело — без токенов: JS веб-клиента их не видит.
    body = r.json()
    assert body["access_token"] is None
    assert body["refresh_token"] is None

    cookies = _set_cookies(r)
    assert cookies.get("access_token"), "нет куки access_token"
    assert cookies.get("refresh_token"), "нет куки refresh_token"

    # Атрибуты: httpOnly + SameSite=strict; refresh ограничен /auth.
    for header in r.headers.get_list("set-cookie"):
        assert "HttpOnly" in header, header
        assert "samesite=strict" in header.lower(), header
        if header.startswith("refresh_token="):
            assert "Path=/auth" in header, header


async def test_login_body_mode_returns_tokens_without_cookies(client):
    r = await _register_and_login(
        client, headers={"X-Token-Delivery": "body"}
    )
    body = r.json()
    assert body["access_token"] and body["refresh_token"]
    assert not r.headers.get_list("set-cookie"), "в body-режиме кук быть не должно"


async def test_access_cookie_authenticates_request(client):
    r = await _register_and_login(client)
    access = _set_cookies(r)["access_token"]

    r = await client.get(
        "/users/me", headers={"Cookie": f"access_token={access}"}
    )
    assert r.status_code == 200, r.text


async def test_refresh_via_cookie_with_rotation(client):
    r = await _register_and_login(client)
    old_refresh = _set_cookies(r)["refresh_token"]

    # Refresh без тела: токен берётся из куки, ответ — новые куки.
    r = await client.post(
        "/auth/refresh",
        json={},
        headers={"Cookie": f"refresh_token={old_refresh}"},
    )
    assert r.status_code == 200, r.text
    new_cookies = _set_cookies(r)
    assert new_cookies["refresh_token"] != old_refresh
    assert new_cookies["access_token"]
    assert r.json()["refresh_token"] is None

    # Rotation: повторный refresh со СТАРОЙ кукой → 401.
    r = await client.post(
        "/auth/refresh",
        json={},
        headers={"Cookie": f"refresh_token={old_refresh}"},
    )
    assert r.status_code == 401


async def test_logout_clears_both_cookies(client):
    r = await _register_and_login(client)
    refresh = _set_cookies(r)["refresh_token"]

    r = await client.post(
        "/auth/logout",
        json={},
        headers={"Cookie": f"refresh_token={refresh}"},
    )
    assert r.status_code == 200, r.text
    cleared = _set_cookies(r)
    # delete_cookie → Set-Cookie с пустым значением и Max-Age/expires в прошлом.
    assert cleared.get("access_token") == ""
    assert cleared.get("refresh_token") == ""


async def test_csrf_rejects_foreign_origin_on_mutation(client):
    r = await client.post(
        "/auth/login",
        json={"email": _email(), "password": PASSWORD},
        headers={"Origin": "https://evil.example"},
    )
    assert r.status_code == 403
    assert r.json()["detail"] == "csrf_origin_mismatch"


async def test_csrf_allows_same_origin(client):
    # Origin совпадает с base_url клиента (http://test) → same-origin, пропуск.
    email = _email()
    r = await client.post(
        "/auth/register",
        json={"email": email, "password": PASSWORD},
        headers={"Origin": "http://test"},
    )
    assert r.status_code == 200, r.text
