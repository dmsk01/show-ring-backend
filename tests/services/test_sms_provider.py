"""SMS-слой: Mock-провайдер, выбор по настройкам, маппинг ошибок sms.ru."""

import logging

import httpx
import pytest

from app.config import settings
from app.services import sms as sms_module
from app.services.sms import (
    MockSMSProvider,
    SMSDeliveryError,
    SmsRuProvider,
    get_sms_provider,
)


async def test_mock_provider_logs_message(caplog):
    provider = MockSMSProvider()
    with caplog.at_level(logging.INFO, logger="app.services.sms"):
        await provider.send("+79991234567", "Ваш код входа: 123456")
    assert "+79991234567" in caplog.text
    assert "123456" in caplog.text


def test_get_sms_provider_defaults_to_mock(monkeypatch):
    monkeypatch.setattr(sms_module, "_provider", None)
    monkeypatch.setattr(settings, "sms_provider", "mock")
    assert isinstance(get_sms_provider(), MockSMSProvider)


def test_get_sms_provider_smsru_requires_key(monkeypatch):
    monkeypatch.setattr(sms_module, "_provider", None)
    monkeypatch.setattr(settings, "sms_provider", "smsru")
    monkeypatch.setattr(settings, "sms_api_key", None)
    with pytest.raises(RuntimeError):
        get_sms_provider()


async def test_smsru_provider_error_status_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"status": "ERROR", "status_code": 202}
        )

    provider = SmsRuProvider("key", transport=httpx.MockTransport(handler))
    with pytest.raises(SMSDeliveryError):
        await provider.send("+79991234567", "code")


async def test_smsru_provider_network_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    provider = SmsRuProvider("key", transport=httpx.MockTransport(handler))
    with pytest.raises(SMSDeliveryError):
        await provider.send("+79991234567", "code")
