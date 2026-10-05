"""
Защита от SMS pumping (накрутки SMS на дорогие номера).

Каждое SMS стоит денег. Атакующий с пулом прокси обходит лимит «5 в минуту
с IP», поэтому нужны ещё три барьера:
- белый список стран — зарубежные премиальные номера отсекаются сразу;
- общий суточный бюджет SMS на весь сервис;
- лимит на подсеть /24: прокси обычно идут пачками из одной сети.
"""

from __future__ import annotations

import random

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.main import app
from app.services.sms import SMSProvider, get_sms_provider


def _phone() -> str:
    return f"+7999{random.randint(1000000, 9999999)}"


class _CaptureSMS(SMSProvider):
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, phone: str, message: str) -> None:
        self.sent.append((phone, message))


@pytest_asyncio.fixture
async def sms():
    provider = _CaptureSMS()
    app.dependency_overrides[get_sms_provider] = lambda: provider
    yield provider
    app.dependency_overrides.pop(get_sms_provider, None)


def _client_from(ip: str) -> AsyncClient:
    """Клиент с заданным IP: ASGITransport кладёт его в request.client."""
    return AsyncClient(
        transport=ASGITransport(app=app, client=(ip, 12345)),
        base_url="http://test",
    )


async def test_foreign_number_rejected_without_sending(client, sms):
    r = await client.post("/auth/send-code", json={"phone": "+442071838750"})

    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "country_not_supported"
    assert sms.sent == []


async def test_daily_budget_stops_sending(client, sms, monkeypatch):
    monkeypatch.setattr(settings, "sms_daily_budget", 2)

    for _ in range(2):
        r = await client.post("/auth/send-code", json={"phone": _phone()})
        assert r.status_code == 200, r.text

    r = await client.post("/auth/send-code", json={"phone": _phone()})
    assert r.status_code == 503, r.text
    assert r.json()["detail"] == "sms_unavailable"
    assert len(sms.sent) == 2


async def test_subnet_limit_spans_neighbour_ips(client, sms, monkeypatch):
    # client-фикстура подменяет get_db/get_redis в app.dependency_overrides —
    # дополнительные клиенты ниже работают поверх тех же подмен.
    monkeypatch.setattr(settings, "otp_subnet_limit", 3)

    async with _client_from("10.20.30.1") as a, _client_from("10.20.30.2") as b:
        for c in (a, b, a):
            r = await c.post("/auth/send-code", json={"phone": _phone()})
            assert r.status_code == 200, r.text

        # Четвёртый запрос из той же /24 — уже с другого адреса — режется,
        # хотя у каждого отдельного IP лимит «5 в минуту» не исчерпан.
        r = await b.post("/auth/send-code", json={"phone": _phone()})
        assert r.status_code == 429, r.text

    async with _client_from("10.20.31.1") as other:
        r = await other.post("/auth/send-code", json={"phone": _phone()})
        assert r.status_code == 200, r.text
