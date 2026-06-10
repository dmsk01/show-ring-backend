"""OTP-сервис: cooldown, суточный лимит, попытки, одноразовость кода.

Redis и репозиторий замоканы (паттерн test_auth_security.py) — тесты
проверяют логику и порядок Redis-команд, не сами хранилища.
"""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

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


CODE = "123456"


def _fake_user(*, is_active: bool = True):
    user = MagicMock()
    user.id = uuid4()
    user.is_active = is_active
    user.roles = []
    return user


def _db():
    db = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.flush = AsyncMock()
    return db


def _redis_with_code(attempts: int = 1):
    return _redis(
        get=AsyncMock(return_value=hash_token(CODE)),
        incr=AsyncMock(return_value=attempts),
    )


# ---------- verify_otp_code ----------


async def test_verify_no_code_raises_expired():
    redis = _redis(get=AsyncMock(return_value=None))

    with pytest.raises(otp_auth.OTPExpiredError):
        await otp_auth.verify_otp_code(_db(), redis, PHONE, CODE)


async def test_verify_wrong_code_raises_invalid_and_keeps_code():
    redis = _redis_with_code(attempts=1)

    with pytest.raises(otp_auth.OTPInvalidError):
        await otp_auth.verify_otp_code(_db(), redis, PHONE, "000000")
    redis.delete.assert_not_called()  # попытки остались — код жив


async def test_verify_third_wrong_attempt_burns_code():
    redis = _redis_with_code(attempts=settings.otp_max_attempts)

    with pytest.raises(otp_auth.OTPInvalidError):
        await otp_auth.verify_otp_code(_db(), redis, PHONE, "000000")
    redis.delete.assert_awaited_once_with(
        f"otp:code:{PHONE}", f"otp:attempts:{PHONE}"
    )


async def test_verify_over_limit_raises_expired():
    redis = _redis_with_code(attempts=settings.otp_max_attempts + 1)

    with pytest.raises(otp_auth.OTPExpiredError):
        await otp_auth.verify_otp_code(_db(), redis, PHONE, CODE)
    redis.delete.assert_awaited()  # код сожжён


async def test_verify_success_existing_user(monkeypatch):
    user = _fake_user()
    redis = _redis_with_code()
    monkeypatch.setattr(
        user_repo, "get_user_by_phone", AsyncMock(return_value=user)
    )
    issue = AsyncMock(return_value="TOKENS")
    monkeypatch.setattr(otp_auth, "issue_token_pair", issue)

    result = await otp_auth.verify_otp_code(_db(), redis, PHONE, CODE)

    assert result == "TOKENS"
    issue.assert_awaited_once()
    redis.delete.assert_awaited()  # код одноразовый


async def test_verify_success_creates_missing_user(monkeypatch):
    new_user = _fake_user()
    redis = _redis_with_code()
    monkeypatch.setattr(
        user_repo, "get_user_by_phone", AsyncMock(return_value=None)
    )
    create = AsyncMock(return_value=new_user)
    monkeypatch.setattr(user_repo, "create_user_by_phone", create)
    monkeypatch.setattr(
        otp_auth, "issue_token_pair", AsyncMock(return_value="TOKENS")
    )

    result = await otp_auth.verify_otp_code(_db(), redis, PHONE, CODE)

    assert result == "TOKENS"
    create.assert_awaited_once()


async def test_verify_blocked_user_rejected(monkeypatch):
    redis = _redis_with_code()
    monkeypatch.setattr(
        user_repo,
        "get_user_by_phone",
        AsyncMock(return_value=_fake_user(is_active=False)),
    )

    with pytest.raises(otp_auth.OTPUserBlockedError):
        await otp_auth.verify_otp_code(_db(), redis, PHONE, CODE)
