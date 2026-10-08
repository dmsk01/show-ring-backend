"""
Метрики безопасности и оповещения (план защиты 2026-10-05, этап 3).

Счётчики событий живут в Redis поминутными корзинами — общие для всех
воркеров uvicorn. Правила раз в минуту смотрят на окно (10 мин / 1 ч) и
шлют оповещение не чаще раза в час на правило.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.services import alerts
from app.services import security_metrics as metrics
from app.models.user import RoleEnum, UserRole
from tests.integration.checkin_helpers import auth, make_api_user

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


async def test_counters_sum_over_window(test_redis):
    await metrics.record(metrics.SMS_SENT, redis=test_redis, now=NOW - timedelta(minutes=5))
    await metrics.record(metrics.SMS_SENT, 2, redis=test_redis, now=NOW)
    # Вне окна в 10 минут — не считается.
    await metrics.record(metrics.SMS_SENT, redis=test_redis, now=NOW - timedelta(minutes=30))

    assert await metrics.total(test_redis, metrics.SMS_SENT, minutes=10, now=NOW) == 3
    assert await metrics.total(test_redis, metrics.SMS_SENT, minutes=60, now=NOW) == 4


async def test_rate_limited_requests_are_counted(client, test_redis):
    # /captcha/challenge: лимит 60 в минуту — 61-й запрос получает 429.
    for _ in range(61):
        r = await client.get("/captcha/challenge")
    assert r.status_code == 429
    assert await metrics.total(test_redis, metrics.RATE_LIMITED, minutes=1) >= 1


@pytest.fixture
def sent(monkeypatch):
    """Перехват доставки: вместо Telegram/почты — список сообщений."""
    messages: list[str] = []

    async def fake_deliver(text: str) -> None:
        messages.append(text)

    monkeypatch.setattr(alerts, "deliver", fake_deliver)
    return messages


async def test_sms_pumping_alert_on_low_conversion(test_redis, sent, monkeypatch):
    monkeypatch.setattr(settings, "alert_sms_min_sent_1h", 10)
    monkeypatch.setattr(settings, "alert_sms_min_conversion", 0.3)
    await metrics.record(metrics.SMS_SENT, 20, redis=test_redis, now=NOW)
    await metrics.record(metrics.OTP_VERIFIED, 2, redis=test_redis, now=NOW)

    fired = await alerts.check_and_send(test_redis, now=NOW)

    assert "sms_conversion" in fired
    assert any("SMS" in m and "10%" in m for m in sent)


async def test_alert_is_not_repeated_within_cooldown(test_redis, sent, monkeypatch):
    monkeypatch.setattr(settings, "alert_rate_limited_10m", 5)
    await metrics.record(metrics.RATE_LIMITED, 10, redis=test_redis, now=NOW)

    first = await alerts.check_and_send(test_redis, now=NOW)
    second = await alerts.check_and_send(test_redis, now=NOW + timedelta(minutes=1))

    assert "rate_limited" in first
    assert "rate_limited" not in second
    assert len(sent) == 1


async def test_quiet_metrics_fire_nothing(test_redis, sent):
    await metrics.record(metrics.SMS_SENT, 3, redis=test_redis, now=NOW)
    await metrics.record(metrics.OTP_VERIFIED, 3, redis=test_redis, now=NOW)

    assert await alerts.check_and_send(test_redis, now=NOW) == []
    assert sent == []


async def test_alert_text_has_no_personal_data(test_redis, sent, monkeypatch):
    # Оповещения уходят во внешний мессенджер — только агрегаты, без
    # телефонов, email и IP (152-ФЗ).
    monkeypatch.setattr(settings, "alert_account_locked_1h", 1)
    await metrics.record(metrics.ACCOUNT_LOCKED, 3, redis=test_redis, now=NOW)

    await alerts.check_and_send(test_redis, now=NOW)

    text = "\n".join(sent)
    assert "@" not in text and "+7" not in text


async def test_admin_can_read_metrics(client, db_session):
    uid, token = await make_api_user(client)
    _, other = await make_api_user(client)
    r = await client.get("/admin/security/metrics", headers=auth(other))
    assert r.status_code == 403

    db_session.add(UserRole(user_id=uid, role=RoleEnum.admin))
    await db_session.commit()
    r = await client.get("/admin/security/metrics", headers=auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body["windows"]) == {"10m", "1h", "24h"}
    assert "sms_sent" in body["windows"]["1h"]


async def test_sms_and_verified_codes_are_counted(client, test_redis, monkeypatch):
    import re

    from app.main import app
    from app.services.sms import SMSProvider, get_sms_provider

    class _Capture(SMSProvider):
        text = ""

        async def send(self, phone: str, message: str) -> None:
            _Capture.text = message

    app.dependency_overrides[get_sms_provider] = lambda: _Capture()
    try:
        phone = "+79990001234"
        await client.post("/auth/send-code", json={"phone": phone})
        code = re.search(r"\d{6}", _Capture.text).group()
        r = await client.post(
            "/auth/verify-code",
            json={
                "phone": phone,
                "code": code,
                "accept_terms": True,
                "personal_data_consent": True,
            },
        )
        assert r.status_code == 200, r.text
    finally:
        app.dependency_overrides.pop(get_sms_provider, None)

    assert await metrics.total(test_redis, metrics.SMS_SENT, minutes=1) == 1
    assert await metrics.total(test_redis, metrics.OTP_VERIFIED, minutes=1) == 1


async def test_middleware_counts_server_errors(test_redis, monkeypatch):
    from httpx import ASGITransport, AsyncClient
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    from app import redis as redis_module
    from app.middleware.metrics import MetricsMiddleware

    monkeypatch.setattr(redis_module, "redis_client", test_redis)

    async def boom(request):
        return PlainTextResponse("down", status_code=503)

    async def ok(request):
        return PlainTextResponse("ok")

    inner = Starlette(routes=[Route("/boom", boom), Route("/ok", ok)])
    async with AsyncClient(
        transport=ASGITransport(app=MetricsMiddleware(inner)), base_url="http://t"
    ) as c:
        await c.get("/ok")
        await c.get("/boom")
        await c.get("/boom")

    assert await metrics.total(test_redis, metrics.HTTP_5XX, minutes=1) == 2

