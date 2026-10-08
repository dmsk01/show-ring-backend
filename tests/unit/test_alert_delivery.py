"""Доставка оповещений в Telegram и на почту (app/services/alerts.py)."""

import json

import httpx

from app.config import settings
from app.services import alerts


def _mock_httpx(monkeypatch, captured: list):
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True})

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(alerts.httpx, "AsyncClient", factory)


async def test_telegram_and_email(monkeypatch):
    captured: list[httpx.Request] = []
    _mock_httpx(monkeypatch, captured)
    emails: list[dict] = []

    async def fake_send_email(**kwargs):
        emails.append(kwargs)

    monkeypatch.setattr(alerts, "send_email", fake_send_email)
    monkeypatch.setattr(settings, "alert_telegram_bot_token", "123:abc")
    monkeypatch.setattr(settings, "alert_telegram_chat_id", "-100500")
    monkeypatch.setattr(settings, "alert_email", "ops@example.com")

    await alerts.deliver("Тест")

    assert len(captured) == 1
    req = captured[0]
    assert req.url.path == "/bot123:abc/sendMessage"
    body = json.loads(req.content)
    assert body == {"chat_id": "-100500", "text": "Show Ring — Тест"}
    assert emails[0]["to_email"] == "ops@example.com"


async def test_no_channels_only_logs(monkeypatch, caplog):
    captured: list[httpx.Request] = []
    _mock_httpx(monkeypatch, captured)
    monkeypatch.setattr(settings, "alert_telegram_bot_token", None)
    monkeypatch.setattr(settings, "alert_telegram_chat_id", None)
    monkeypatch.setattr(settings, "alert_email", None)

    with caplog.at_level("WARNING", logger="app.security"):
        await alerts.deliver("Тихо")

    assert captured == []
    assert "security_alert Тихо" in caplog.text
