"""OTP-сервис: cooldown, суточный лимит, попытки, одноразовость кода.

Redis и репозиторий замоканы (паттерн test_auth_security.py) — тесты
проверяют логику и порядок Redis-команд, не сами хранилища.
"""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.config import settings
from app.repositories import user as user_repo
from app.schemas.user import TokenResponse
from app.services import consent as consent_svc
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
    r.delete = AsyncMock(return_value=1)
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
        if c.args[0] == f"otp:login:code:{PHONE}"
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


def _tokens():
    return TokenResponse(access_token="A", refresh_token="R", token_type="bearer")


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
        f"otp:login:code:{PHONE}", f"otp:login:attempts:{PHONE}"
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
    issue = AsyncMock(return_value=_tokens())
    monkeypatch.setattr(otp_auth, "issue_token_pair", issue)

    tokens, is_new_user = await otp_auth.verify_otp_code(
        _db(), redis, PHONE, CODE
    )

    assert tokens.access_token == "A"
    assert is_new_user is False
    assert tokens.is_new_user is False
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
        otp_auth, "issue_token_pair", AsyncMock(return_value=_tokens())
    )
    grant = AsyncMock()
    monkeypatch.setattr(consent_svc, "grant", grant)

    tokens, is_new_user = await otp_auth.verify_otp_code(
        _db(), redis, PHONE, CODE, consents=consent_svc.ACCOUNT_KINDS
    )

    assert is_new_user is True
    assert tokens.is_new_user is True
    create.assert_awaited_once()
    assert grant.await_count == len(consent_svc.ACCOUNT_KINDS)


async def test_verify_new_user_without_consents_not_created(monkeypatch):
    redis = _redis_with_code()
    monkeypatch.setattr(
        user_repo, "get_user_by_phone", AsyncMock(return_value=None)
    )
    create = AsyncMock()
    monkeypatch.setattr(user_repo, "create_user_by_phone", create)

    with pytest.raises(consent_svc.ConsentRequiredError):
        await otp_auth.verify_otp_code(
            _db(), redis, PHONE, CODE, consents=(consent_svc.ConsentKind.terms,)
        )
    create.assert_not_awaited()


async def test_verify_blocked_user_rejected(monkeypatch):
    redis = _redis_with_code()
    monkeypatch.setattr(
        user_repo,
        "get_user_by_phone",
        AsyncMock(return_value=_fake_user(is_active=False)),
    )

    with pytest.raises(otp_auth.OTPUserBlockedError):
        await otp_auth.verify_otp_code(_db(), redis, PHONE, CODE)


async def test_verify_race_condition_raises_expired(monkeypatch):
    # DEL вернул 0 — параллельный верный запрос уже «съел» ключ.
    # Второй запрос должен получить OTPExpiredError, а не выдать токены.
    redis = _redis_with_code()
    redis.delete = AsyncMock(return_value=0)  # код уже удалён параллельным запросом

    with pytest.raises(otp_auth.OTPExpiredError):
        await otp_auth.verify_otp_code(_db(), redis, PHONE, CODE)


# ---------- цели OTP (purpose) ----------


def _stored_code_keys(redis):
    return [
        c.args[0]
        for c in redis.set.await_args_list
        if ":code:" in c.args[0]
    ]


async def test_send_reauth_uses_purpose_and_subject_keys():
    redis, sms = _redis(), _sms()
    user_id = str(uuid4())

    await otp_auth.send_otp_code(
        redis, sms, PHONE, purpose=otp_auth.OTPPurpose.reauth, subject=user_id
    )

    # Код привязан к субъекту (user_id), cooldown — к (цели, номеру),
    # суточный лимит — общий на номер.
    assert _stored_code_keys(redis) == [f"otp:reauth:code:{user_id}"]
    cooldown_key = redis.set.await_args_list[0].args[0]
    assert cooldown_key == f"otp:reauth:cooldown:{PHONE}"
    # INCR-ов два: суточный лимит номера и общий бюджет SMS сервиса.
    incr_keys = [c.args[0] for c in redis.incr.await_args_list]
    assert incr_keys[0] == f"otp:daily:{PHONE}"
    assert incr_keys[1].startswith("otp:budget:")
    # SMS уходит на номер, текст — под цель.
    sent_phone, message = sms.send.await_args.args
    assert sent_phone == PHONE
    assert message.startswith("Код подтверждения:")


async def test_consume_reads_only_its_purpose():
    # Код цели login не принимается как reauth: читается ключ своей цели.
    redis = _redis(get=AsyncMock(return_value=None))

    with pytest.raises(otp_auth.OTPExpiredError):
        await otp_auth.consume_otp_code(
            redis, CODE, purpose=otp_auth.OTPPurpose.reauth, subject="u1"
        )
    redis.get.assert_awaited_once_with("otp:reauth:code:u1")


async def test_consume_success_burns_code():
    redis = _redis_with_code()

    await otp_auth.consume_otp_code(
        redis, CODE, purpose=otp_auth.OTPPurpose.link_phone, subject="u1:+7"
    )

    redis.delete.assert_any_await("otp:link_phone:code:u1:+7")


async def test_send_rejects_foreign_number_before_any_counter(monkeypatch):
    monkeypatch.setattr(settings, "sms_allowed_phone_prefixes", ["+7"])
    redis, sms = _redis(), _sms()

    with pytest.raises(otp_auth.OTPCountryNotAllowedError):
        await otp_auth.send_otp_code(redis, sms, "+442071838750")

    # Ни cooldown, ни счётчики не тронуты, SMS не ушло.
    redis.set.assert_not_awaited()
    redis.incr.assert_not_awaited()
    sms.send.assert_not_awaited()


async def test_budget_threshold_is_logged(monkeypatch, caplog):
    monkeypatch.setattr(settings, "sms_daily_budget", 10)
    # Суточный счётчик номера = 1, бюджет сервиса = 8 → порог 80%.
    redis = _redis(incr=AsyncMock(side_effect=[1, 8]))

    with caplog.at_level("WARNING", logger="app.security"):
        await otp_auth.send_otp_code(redis, _sms(), PHONE)

    assert "sms_budget_threshold share=80%" in caplog.text
