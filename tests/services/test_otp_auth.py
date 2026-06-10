"""OTP-сервис: cooldown, суточный лимит, попытки, одноразовость кода.

Redis и репозиторий замоканы (паттерн test_auth_security.py) — тесты
проверяют логику и порядок Redis-команд, не сами хранилища.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.repositories import user as user_repo
from app.services import otp_auth
from app.utils.security import hash_token

PHONE = "+79991234567"


def _redis(**overrides):
    """AsyncMock Redis: по умолчанию — «чистое» состояние."""
    r = MagicMock()
    r.set = AsyncMock(return_value=True)       # SET NX EX прошёл
    r.get = AsyncMock(return_value=None)
    r.incr = AsyncMock(return_value=1)
    r.expire = AsyncMock()
    r.delete = AsyncMock()
    for name, value in overrides.items():
        setattr(r, name, value)
    return r


def _sms():
    sms = MagicMock()
    sms.send = AsyncMock()
    return sms


# ---------- send_otp_code ----------


async def test_send_stores_hash_and_sends_sms():
    redis, sms = _redis(), _sms()

    await otp_auth.send_otp_code(redis, sms, PHONE)

    sms.send.assert_awaited_once()
    sent_phone, message = sms.send.await_args.args
    assert sent_phone == PHONE
    # В Redis ушёл ХЕШ кода из SMS, с TTL из настроек.
    code = next(
        w for w in message.split() if w.isdigit()
    )
    stored = [
        c for c in redis.set.await_args_list
        if c.args[0] == f"otp:code:{PHONE}"
    ]
    assert stored[0].args[1] == hash_token(code)
    assert stored[0].kwargs["ex"] == settings.otp_code_ttl_seconds


async def test_send_cooldown_raises_rate_limited():
    # SET NX вернул None → SMS уже уходило < cooldown назад.
    redis, sms = _redis(set=AsyncMock(return_value=None)), _sms()

    with pytest.raises(otp_auth.OTPRateLimitedError):
        await otp_auth.send_otp_code(redis, sms, PHONE)
    sms.send.assert_not_called()


async def test_send_daily_limit_raises_rate_limited():
    redis, sms = _redis(), _sms()
    redis.incr = AsyncMock(return_value=settings.otp_daily_limit + 1)

    with pytest.raises(otp_auth.OTPRateLimitedError):
        await otp_auth.send_otp_code(redis, sms, PHONE)
    sms.send.assert_not_called()
