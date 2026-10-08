"""
Unit: проверка prod-конфигурации при старте (ревью 2026-10-06, BE-38).

Раньше SMS_PROVIDER=mock при DEBUG=False обнаруживался только при первом
/auth/send-code (пользователь получал 500), а дефолтные учётки S3 и
локальный SMTP в проде не проверялись вовсе.
"""

from __future__ import annotations

import pytest

from app.config import production_problems, settings


def _prod(monkeypatch, **overrides):
    values = dict(
        debug=False, sms_provider="smsru", sms_api_key="k",
        s3_access_key="prod-key", s3_secret_key="prod-secret",
        # Прод за nginx — фронт и API на одном origin, CORS не нужен
        # (.env.prod.example его не задаёт): пустой список — норма.
        smtp_host="smtp.example.com", cors_allow_origins=[],
        captcha_enabled=True,
    )
    values.update(overrides)
    for k, v in values.items():
        monkeypatch.setattr(settings, k, v)


def test_clean_production_config(monkeypatch):
    _prod(monkeypatch)
    assert production_problems(settings) == []


@pytest.mark.parametrize(
    "override, needle",
    [
        ({"sms_provider": "mock"}, "SMS_PROVIDER"),
        ({"sms_api_key": None}, "SMS_API_KEY"),
        ({"s3_secret_key": "show_ring_minio"}, "S3"),
        ({"smtp_host": "127.0.0.1"}, "SMTP_HOST"),
        ({"captcha_enabled": False}, "CAPTCHA"),
    ],
)
def test_detects_unsafe_production_settings(monkeypatch, override, needle):
    _prod(monkeypatch, **override)
    problems = production_problems(settings)
    assert any(needle in p for p in problems), problems


def test_debug_mode_skips_checks(monkeypatch):
    _prod(monkeypatch, debug=True, sms_provider="mock")
    assert production_problems(settings) == []
