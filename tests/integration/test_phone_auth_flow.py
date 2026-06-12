"""
Интеграция phone-OTP: HTTP-стек + PostgreSQL + Redis.

SMS перехватывается подменой get_sms_provider через dependency_overrides —
тест достаёт код из «отправленного» сообщения, как это сделал бы телефон.
"""

from __future__ import annotations

import random
import re

import pytest_asyncio

from app.main import app
from app.services.sms import SMSProvider, get_sms_provider


def _phone() -> str:
    return f"+7999{random.randint(1000000, 9999999)}"


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


async def test_full_flow_creates_user_and_logs_in(client, sms_capture):
    phone = _phone()

    # 1. Отправка кода.
    r = await client.post("/auth/send-code", json={"phone": phone})
    assert r.status_code == 200, r.text
    code = sms_capture.last_code()

    # 2. Верный код → пара токенов, пользователь создан. Заголовок
    # X-Token-Delivery: body — «мобильный» режим: токены в теле ответа.
    r = await client.post(
        "/auth/verify-code",
        json={"phone": phone, "code": code},
        headers={"X-Token-Delivery": "body"},
    )
    assert r.status_code == 200, r.text
    tokens = r.json()
    assert tokens["access_token"] and tokens["refresh_token"]

    # 3. Access-токен работает; у телефонного юзера email пуст.
    r = await client.get(
        "/users/me",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["email"] is None

    # 4. Код одноразовый: повторный verify тем же кодом → 401.
    r = await client.post(
        "/auth/verify-code", json={"phone": phone, "code": code}
    )
    assert r.status_code == 401

    # 5. Refresh-токен принимается стандартным /auth/refresh.
    r = await client.post(
        "/auth/refresh",
        json={"refresh_token": tokens["refresh_token"]},
        headers={"X-Token-Delivery": "body"},
    )
    assert r.status_code == 200, r.text


async def test_second_login_reuses_user(client, sms_capture, test_redis):
    phone = _phone()
    await client.post("/auth/send-code", json={"phone": phone})
    code = sms_capture.last_code()
    r1 = await client.post(
        "/auth/verify-code",
        json={"phone": phone, "code": code},
        headers={"X-Token-Delivery": "body"},
    )
    assert r1.status_code == 200

    # Снимаем cooldown (как будто прошла минута) и входим повторно.
    await test_redis.delete(f"otp:cooldown:{phone}")
    await client.post("/auth/send-code", json={"phone": phone})
    code2 = sms_capture.last_code()
    r2 = await client.post(
        "/auth/verify-code",
        json={"phone": phone, "code": code2},
        headers={"X-Token-Delivery": "body"},
    )
    assert r2.status_code == 200

    # Один и тот же пользователь (sub в JWT), а не дубликат.
    from jose import jwt

    sub1 = jwt.get_unverified_claims(r1.json()["access_token"])["sub"]
    sub2 = jwt.get_unverified_claims(r2.json()["access_token"])["sub"]
    assert sub1 == sub2


async def test_cooldown_returns_429(client, sms_capture):
    phone = _phone()
    r = await client.post("/auth/send-code", json={"phone": phone})
    assert r.status_code == 200
    r = await client.post("/auth/send-code", json={"phone": phone})
    assert r.status_code == 429


async def test_three_wrong_attempts_burn_code(client, sms_capture):
    phone = _phone()
    await client.post("/auth/send-code", json={"phone": phone})
    code = sms_capture.last_code()
    wrong = "000000" if code != "000000" else "111111"

    for _ in range(3):
        r = await client.post(
            "/auth/verify-code", json={"phone": phone, "code": wrong}
        )
        assert r.status_code == 400

    # Код сожжён — даже ВЕРНЫЙ код теперь даёт 401.
    r = await client.post(
        "/auth/verify-code", json={"phone": phone, "code": code}
    )
    assert r.status_code == 401


async def test_invalid_phone_format_422(client, sms_capture):
    r = await client.post("/auth/send-code", json={"phone": "89991234567"})
    assert r.status_code == 422
