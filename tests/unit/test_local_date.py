"""
Unit: «сегодня» считается в часовом поясе сервиса, а не сервера
(ревью 2026-10-06, BE-30).

Контейнер работает в UTC: регистрация с дедлайном «15 октября» по Москве
фактически закрывалась в 03:00 16-го, показ рекламы переключался по дате
в 03:00 МСК.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from app.config import settings
from app.utils import dates


def test_today_local_uses_service_timezone(monkeypatch):
    monkeypatch.setattr(settings, "app_timezone", "Europe/Moscow")
    # 15 октября 22:30 UTC = 16 октября 01:30 МСК.
    monkeypatch.setattr(
        dates, "_utcnow", lambda: datetime(2026, 10, 15, 22, 30, tzinfo=timezone.utc)
    )
    assert dates.today_local() == date(2026, 10, 16)


def test_today_local_other_timezone(monkeypatch):
    monkeypatch.setattr(settings, "app_timezone", "UTC")
    monkeypatch.setattr(
        dates, "_utcnow", lambda: datetime(2026, 10, 15, 22, 30, tzinfo=timezone.utc)
    )
    assert dates.today_local() == date(2026, 10, 15)
