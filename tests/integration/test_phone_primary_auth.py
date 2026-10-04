"""
Интеграция «телефон — основной способ»: реестр методов, закрытая
email-регистрация, привязка телефона, подключение входа по почте.

HTTP-стек + PostgreSQL + Redis; SMS перехватывается подменой
get_sms_provider (как в test_phone_auth_flow.py).
"""

from __future__ import annotations

import random
import re

import pytest_asyncio

from app.config import settings
from app.main import app
from app.services.sms import SMSProvider, get_sms_provider

BODY = {"X-Token-Delivery": "body"}


def _phone() -> str:
    return f"+7998{random.randint(1000000, 9999999)}"


class _CaptureSMS(SMSProvider):
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, phone: str, message: str) -> None:
        self.sent.append((phone, message))

    def last_code(self) -> str:
        match = re.search(r"\d{4,8}", self.sent[-1][1])
        assert match, f"в SMS нет кода: {self.sent[-1][1]!r}"
        return match.group()


@pytest_asyncio.fixture
async def sms_capture():
    provider = _CaptureSMS()
    app.dependency_overrides[get_sms_provider] = lambda: provider
    yield provider
    app.dependency_overrides.pop(get_sms_provider, None)


async def _phone_login(client, sms, phone: str) -> tuple[dict, dict]:
    r = await client.post("/auth/send-code", json={"phone": phone})
    assert r.status_code == 200, r.text
    r = await client.post(
        "/auth/verify-code",
        json={
            "phone": phone,
            "code": sms.last_code(),
            "accept_terms": True, "personal_data_consent": True,
        },
        headers=BODY,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    return body, {"Authorization": f"Bearer {body['access_token']}"}


async def _email_user(client) -> tuple[str, dict]:
    email = f"legacy{random.randint(10**6, 10**7)}@example.com"
    r = await client.post(
        "/auth/register", json={"email": email, "password": "Password123"}
    )
    assert r.status_code == 200, r.text
    r = await client.post(
        "/auth/login",
        json={"email": email, "password": "Password123"},
        headers=BODY,
    )
    assert r.status_code == 200, r.text
    return email, {"Authorization": f"Bearer {r.json()['access_token']}"}


# ---------- реестр методов и флаги ----------


async def test_auth_methods_phone_primary(client):
    r = await client.get("/auth/methods")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["primary"] == "phone_otp"
    assert data["methods"][0]["id"] == "phone_otp"
    # Фикстура включила email-регистрацию — реестр это отражает.
    email = next(m for m in data["methods"] if m["id"] == "email_password")
    assert email["sign_up"] is True


async def test_email_registration_disabled_403(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_email_registration_enabled", False)
    r = await client.post(
        "/auth/register",
        json={"email": "nobody@example.com", "password": "Password123"},
    )
    assert r.status_code == 403
    assert r.json()["detail"] == "registration_method_disabled"


async def test_email_login_disabled_403(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_email_login_enabled", False)
    r = await client.post(
        "/auth/login",
        json={"email": "nobody@example.com", "password": "Password123"},
    )
    assert r.status_code == 403
    assert r.json()["detail"] == "login_method_disabled"


async def test_verify_code_reports_new_user(client, sms_capture, test_redis):
    phone = _phone()
    first, _ = await _phone_login(client, sms_capture, phone)
    assert first["is_new_user"] is True

    await test_redis.delete(f"otp:login:cooldown:{phone}")
    second, _ = await _phone_login(client, sms_capture, phone)
    assert second["is_new_user"] is False


# ---------- привязка телефона (legacy email-пользователь) ----------


async def test_legacy_user_links_phone(client, sms_capture):
    _, auth = await _email_user(client)
    phone = _phone()

    r = await client.post(
        "/users/me/phone/send-code", json={"phone": phone}, headers=auth
    )
    assert r.status_code == 200, r.text
    code = sms_capture.last_code()

    # Неверный код — 400, не 401 (иначе фронт разлогинит пользователя).
    wrong = "000000" if code != "000000" else "111111"
    r = await client.post(
        "/users/me/phone/verify",
        json={"phone": phone, "code": wrong},
        headers=auth,
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid_code"

    r = await client.post(
        "/users/me/phone/verify",
        json={"phone": phone, "code": code},
        headers=auth,
    )
    assert r.status_code == 200, r.text
    assert r.json()["phone"] == phone
    assert r.json()["is_phone_verified"] is True

    # Номер уже есть — повторная привязка запрещена.
    r = await client.post(
        "/users/me/phone/send-code", json={"phone": _phone()}, headers=auth
    )
    assert r.status_code == 409
    assert r.json()["detail"] == "phone_already_set"


async def test_link_phone_taken_by_other_account(client, sms_capture):
    phone = _phone()
    await _phone_login(client, sms_capture, phone)  # номер занят телефонным юзером
    _, auth = await _email_user(client)

    r = await client.post(
        "/users/me/phone/send-code", json={"phone": phone}, headers=auth
    )
    assert r.status_code == 409
    assert r.json()["detail"] == "phone_taken"


# ---------- подключение входа по почте (телефонный пользователь) ----------


async def test_phone_user_adds_email_login(client, sms_capture):
    phone = _phone()
    _, auth = await _phone_login(client, sms_capture, phone)

    r = await client.get("/users/me", headers=auth)
    assert r.json()["has_password"] is False

    r = await client.post("/users/me/reauth/send-code", headers=auth)
    assert r.status_code == 200, r.text
    code = sms_capture.last_code()
    assert sms_capture.sent[-1][0] == phone  # код — на номер аккаунта

    email = f"phone{random.randint(10**6, 10**7)}@example.com"
    r = await client.post(
        "/users/me/email-login",
        json={"email": email, "password": "Password123", "code": code},
        headers=auth,
    )
    assert r.status_code == 200, r.text

    r = await client.get("/users/me", headers=auth)
    me = r.json()
    assert me["has_password"] is True
    assert me["pending_email"] == email
    # Вход по почте — только после подтверждения ссылки.
    assert me["email"] is None
    r = await client.post(
        "/auth/login", json={"email": email, "password": "Password123"}
    )
    assert r.status_code == 401


async def test_add_email_login_requires_fresh_code(client, sms_capture):
    _, auth = await _phone_login(client, sms_capture, _phone())

    r = await client.post(
        "/users/me/email-login",
        json={"email": "x@example.com", "password": "Password123", "code": "123456"},
        headers=auth,
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "code_expired"
