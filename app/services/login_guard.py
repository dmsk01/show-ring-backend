"""
Защита входа по паролю от подбора (план защиты 2026-10-05, этап 2).

Лимит «N запросов в минуту с IP» не останавливает распределённый перебор
пароля одного аккаунта с тысяч адресов. Поэтому считаем неудачи ещё и по
email:

- после `login_captcha_after_failures` неудач (с IP или для email) вход
  требует решённую капчу;
- после `login_lockout_failures` неудач для email вход в этот аккаунт
  закрыт на `login_lockout_seconds`, владельцу уходит письмо.

Счётчики ведутся по строке email, а не по найденному пользователю: ответы
для существующего и несуществующего адреса одинаковы (анти-enumeration).

Ключи Redis:
  login:fail:email:{email}  — неудачи для адреса, TTL = окно блокировки
  login:fail:ip:{ip}        — неудачи с IP, тот же TTL
  login:lock:{email}        — активная блокировка
"""

from __future__ import annotations

import logging

from fastapi import HTTPException
from redis.asyncio import Redis

from app.config import settings
from app.services import security_metrics
from app.services.captcha import require_captcha

security_logger = logging.getLogger("app.security")


def _norm(email: str) -> str:
    return email.strip().lower()


def _email_key(email: str) -> str:
    return f"login:fail:email:{_norm(email)}"


def _ip_key(ip: str) -> str:
    return f"login:fail:ip:{ip}"


def _lock_key(email: str) -> str:
    return f"login:lock:{_norm(email)}"


async def check_before_login(
    redis: Redis, *, email: str, ip: str, captcha: str | None
) -> None:
    """До проверки пароля: блокировка аккаунта и при необходимости капча."""
    lock_ttl = await redis.ttl(_lock_key(email))
    if lock_ttl and lock_ttl > 0:
        raise HTTPException(
            status_code=429,
            detail="account_locked",
            headers={"Retry-After": str(lock_ttl)},
        )

    email_fails = int(await redis.get(_email_key(email)) or 0)
    ip_fails = int(await redis.get(_ip_key(ip)) or 0)
    if max(email_fails, ip_fails) >= settings.login_captcha_after_failures:
        await require_captcha(redis, captcha)


async def record_failure(redis: Redis, *, email: str, ip: str) -> bool:
    """Учесть неудачный вход. True — аккаунт только что заблокирован."""
    window = settings.login_lockout_seconds
    await security_metrics.record(security_metrics.LOGIN_FAILED, redis=redis)
    fails = await redis.incr(_email_key(email))
    if fails == 1:
        await redis.expire(_email_key(email), window)
    ip_fails = await redis.incr(_ip_key(ip))
    if ip_fails == 1:
        await redis.expire(_ip_key(ip), window)

    if fails >= settings.login_lockout_failures:
        locked = await redis.set(_lock_key(email), "1", nx=True, ex=window)
        await redis.delete(_email_key(email))
        if locked:
            security_logger.warning(
                "login_account_locked email=%s ip=%s", _norm(email), ip
            )
            await security_metrics.record(security_metrics.ACCOUNT_LOCKED, redis=redis)
            return True
    return False


async def record_success(redis: Redis, *, email: str) -> None:
    """Успешный вход обнуляет счётчик аккаунта (IP-счётчик живёт своё окно)."""
    await redis.delete(_email_key(email))
