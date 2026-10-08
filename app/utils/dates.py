"""
«Сегодня» в часовом поясе сервиса (ревью 2026-10-06, BE-30).

Контейнер работает в UTC, а даты выставок, дедлайнов регистрации и
рекламных кампаний — московские (settings.app_timezone). date.today()
сервера сдвигал границы дня на 3 часа: регистрация с дедлайном
«15 октября» закрывалась в 03:00 16-го.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from app.config import settings


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def today_local() -> date:
    """Текущая дата в settings.app_timezone."""
    return _utcnow().astimezone(ZoneInfo(settings.app_timezone)).date()
