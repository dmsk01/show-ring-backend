"""
Счётчики событий безопасности (план защиты 2026-10-05, этап 3).

Зачем не Prometheus: на одном VPS отдельный стек мониторинга избыточен,
а uvicorn работает в нескольких процессах — счётчики в памяти процесса
расходились бы. Redis общий для всех воркеров, поэтому событие пишется
INCR в поминутную корзину:

    metrics:{event}:{YYYYmmddHHMM}   TTL двое суток

Сумма за окно — MGET по корзинам окна. Запись — fire-and-forget: сбой
Redis не должен ломать запрос, ради которого считаем событие.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from redis.asyncio import Redis

from app import redis as redis_module

logger = logging.getLogger(__name__)

# События. Строки — часть ключей Redis и ответа админского эндпоинта.
RATE_LIMITED = "rate_limited"      # ответ 429 от check_rate_limit
HTTP_5XX = "http_5xx"              # ответ сервера 5xx
SLOW_REQUEST = "slow_request"      # запрос дольше slow_request_seconds
SMS_SENT = "sms_sent"              # SMS с кодом ушло провайдеру
OTP_VERIFIED = "otp_verified"      # код из SMS введён верно
CAPTCHA_FAILED = "captcha_failed"  # капча отвергнута
LOGIN_FAILED = "login_failed"      # неверный пароль
ACCOUNT_LOCKED = "account_locked"  # аккаунт заблокирован после серии ошибок

ALL_EVENTS = (
    RATE_LIMITED,
    HTTP_5XX,
    SLOW_REQUEST,
    SMS_SENT,
    OTP_VERIFIED,
    CAPTCHA_FAILED,
    LOGIN_FAILED,
    ACCOUNT_LOCKED,
)

_TTL_SECONDS = 2 * 86400


def _bucket(event: str, at: datetime) -> str:
    return f"metrics:{event}:{at:%Y%m%d%H%M}"


async def record(
    event: str,
    n: int = 1,
    *,
    redis: Redis | None = None,
    now: datetime | None = None,
) -> None:
    """Учесть событие. Без Redis или при ошибке — молча пропускаем."""
    client = redis or redis_module.redis_client
    if client is None:
        return
    key = _bucket(event, now or datetime.now(timezone.utc))
    try:
        await client.incrby(key, n)
        await client.expire(key, _TTL_SECONDS)
    except Exception:  # noqa: BLE001 — метрика не должна ронять запрос
        logger.debug("metrics.record failed for %s", event, exc_info=True)


async def total(
    redis: Redis, event: str, *, minutes: int, now: datetime | None = None
) -> int:
    """Сумма события за последние `minutes` минут, включая текущую."""
    end = (now or datetime.now(timezone.utc)).replace(second=0, microsecond=0)
    keys = [_bucket(event, end - timedelta(minutes=i)) for i in range(minutes)]
    values = await redis.mget(keys)
    return sum(int(v) for v in values if v is not None)


async def summary(redis: Redis, *, now: datetime | None = None) -> dict:
    """Все события за 10 минут, час и сутки — для админского эндпоинта."""
    windows = {"10m": 10, "1h": 60, "24h": 1440}
    return {
        name: {e: await total(redis, e, minutes=m, now=now) for e in ALL_EVENTS}
        for name, m in windows.items()
    }
