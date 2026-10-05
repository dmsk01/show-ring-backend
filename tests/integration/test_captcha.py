"""
Капча ALTCHA (proof-of-work, работает на нашем сервере) и защита входа
по паролю (план защиты 2026-10-05, этап 2).

- /auth/send-code требует решённую задачу всегда: каждое SMS стоит денег;
- вход по паролю требует капчу после N неудачных попыток с IP или для
  email, а после M неудач блокирует вход в аккаунт на время.
"""

from __future__ import annotations

import random
import uuid

import pytest
import pytest_asyncio
from altcha.v2 import Challenge, Payload, solve_challenge

from sqlalchemy import select

from app.config import settings
from app.models.notification import Notification
from app.main import app
from app.services.sms import SMSProvider, get_sms_provider

PASSWORD = "secret123"


def _phone() -> str:
    return f"+7999{random.randint(1000000, 9999999)}"


class _CaptureSMS(SMSProvider):
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, phone: str, message: str) -> None:
        self.sent.append(phone)


@pytest.fixture(autouse=True)
def _captcha_on(monkeypatch):
    # В остальных интеграционных тестах капча выключена (conftest);
    # здесь — включена и дешёвая, чтобы решение в тесте занимало миг.
    monkeypatch.setattr(settings, "captcha_enabled", True)
    monkeypatch.setattr(settings, "captcha_cost", 1)


@pytest_asyncio.fixture
async def sms():
    provider = _CaptureSMS()
    app.dependency_overrides[get_sms_provider] = lambda: provider
    yield provider
    app.dependency_overrides.pop(get_sms_provider, None)


async def _solved(client) -> str:
    """Получить задачу у сервера и решить её, как это делает виджет."""
    r = await client.get("/captcha/challenge")
    assert r.status_code == 200, r.text
    challenge = Challenge.from_dict(r.json())
    solution = solve_challenge(challenge)
    assert solution is not None
    return Payload(challenge=challenge, solution=solution).to_base64()


# --- SMS ----------------------------------------------------------------


async def test_challenge_is_signed(client):
    r = await client.get("/captcha/challenge")
    body = r.json()
    assert body["signature"]
    assert body["parameters"]["algorithm"] == "PBKDF2/SHA-256"
    assert body["parameters"]["expiresAt"] > 0


async def test_send_code_requires_captcha(client, sms):
    r = await client.post("/auth/send-code", json={"phone": _phone()})
    assert r.status_code == 400
    assert r.json()["detail"] == "captcha_required"

    r = await client.post(
        "/auth/send-code", json={"phone": _phone(), "captcha": "bm90LWEtcGF5bG9hZA=="}
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "captcha_invalid"
    assert sms.sent == []


async def test_send_code_with_solved_captcha(client, sms):
    payload = await _solved(client)
    r = await client.post(
        "/auth/send-code", json={"phone": _phone(), "captcha": payload}
    )
    assert r.status_code == 200, r.text
    assert len(sms.sent) == 1

    # Одно решение — один запрос: повтор того же payload отвергается.
    r = await client.post(
        "/auth/send-code", json={"phone": _phone(), "captcha": payload}
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "captcha_invalid"
    assert len(sms.sent) == 1


async def test_tampered_challenge_rejected(client, sms):
    r = await client.get("/captcha/challenge")
    data = r.json()
    # Подменили сложность на нулевую — подпись сервера больше не сходится.
    data["parameters"]["keyPrefix"] = ""
    challenge = Challenge.from_dict(data)
    solution = solve_challenge(challenge)
    payload = Payload(challenge=challenge, solution=solution).to_base64()

    r = await client.post(
        "/auth/send-code", json={"phone": _phone(), "captcha": payload}
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "captcha_invalid"


# --- Вход по паролю -------------------------------------------------------


async def _register(client) -> str:
    email = f"guard_{uuid.uuid4().hex[:10]}@example.com"
    r = await client.post(
        "/auth/register", json={"email": email, "password": PASSWORD}
    )
    assert r.status_code == 200, r.text
    return email


async def _login(client, email, password, captcha=None):
    body = {"email": email, "password": password}
    if captcha:
        body["captcha"] = captcha
    return await client.post("/auth/login", json=body)


async def test_login_captcha_after_failures(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_login_rate_limit", 100)
    monkeypatch.setattr(settings, "login_captcha_after_failures", 2)
    email = await _register(client)

    # Первые попытки — без капчи.
    assert (await _login(client, email, "wrong-pass-1")).status_code == 401
    assert (await _login(client, email, "wrong-pass-2")).status_code == 401

    r = await _login(client, email, PASSWORD)
    assert r.status_code == 400
    assert r.json()["detail"] == "captcha_required"

    r = await _login(client, email, PASSWORD, await _solved(client))
    assert r.status_code == 200, r.text

    # Успешный вход сбрасывает счётчик аккаунта; IP-счётчик живёт своё окно.


async def test_account_lockout(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "auth_login_rate_limit", 100)
    monkeypatch.setattr(settings, "login_captcha_after_failures", 100)
    monkeypatch.setattr(settings, "login_lockout_failures", 3)
    email = await _register(client)

    for i in range(3):
        assert (await _login(client, email, f"wrong-{i}x")).status_code == 401

    # Даже верный пароль не пускает, пока действует блокировка.
    r = await _login(client, email, PASSWORD)
    assert r.status_code == 429
    assert r.json()["detail"] == "account_locked"
    assert int(r.headers["Retry-After"]) > 0

    # Владельцу поставлено письмо о блокировке.
    notes = await db_session.execute(
        select(Notification).where(
            Notification.event_type == "transactional.account_locked"
        )
    )
    assert notes.scalars().first() is not None

    # Другой аккаунт с того же IP не заблокирован.
    other = await _register(client)
    assert (await _login(client, other, PASSWORD)).status_code == 200


async def test_lockout_counts_unknown_emails_too(client, monkeypatch):
    # Блокировка не должна выдавать, существует ли адрес: для
    # несуществующего email ответ тот же.
    monkeypatch.setattr(settings, "auth_login_rate_limit", 100)
    monkeypatch.setattr(settings, "login_captcha_after_failures", 100)
    monkeypatch.setattr(settings, "login_lockout_failures", 2)
    ghost = f"ghost_{uuid.uuid4().hex[:8]}@example.com"

    for _ in range(2):
        assert (await _login(client, ghost, "whatever1")).status_code == 401
    r = await _login(client, ghost, "whatever1")
    assert r.status_code == 429
    assert r.json()["detail"] == "account_locked"
