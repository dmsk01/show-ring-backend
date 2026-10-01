"""Привязка телефона и подключение входа по почте (services/account_security).

Redis-OTP, репозиторий, аудит и очередь писем замоканы — проверяется
бизнес-логика: коды ошибок, порядок проверок, что пишется в пользователя.
"""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from app.repositories import security_audit as audit_repo
from app.repositories import user as user_repo
from app.services import account_security as svc
from app.services.otp_auth import OTPExpiredError, OTPInvalidError, OTPPurpose

PHONE = "+79991234567"
CODE = "123456"


def _user(*, phone=None, phone_verified=False, email=None, password=None):
    user = MagicMock()
    user.id = uuid4()
    user.phone = phone
    user.is_phone_verified = phone_verified
    user.email = email
    user.hashed_password = password
    user.pending_email = None
    return user


def _db():
    db = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


@pytest.fixture
def otp(monkeypatch):
    """Мок OTP-слоя: send/consume по умолчанию успешны."""
    send = AsyncMock()
    consume = AsyncMock()
    monkeypatch.setattr(svc, "send_otp_code", send)
    monkeypatch.setattr(svc, "consume_otp_code", consume)
    return MagicMock(send=send, consume=consume)


@pytest.fixture
def side_effects(monkeypatch):
    audit = AsyncMock()
    email = AsyncMock()
    token = AsyncMock()
    monkeypatch.setattr(audit_repo, "record_security_event", audit)
    monkeypatch.setattr(svc, "enqueue_transactional_email", email)
    monkeypatch.setattr(user_repo, "create_email_verification_token", token)
    return MagicMock(audit=audit, email=email, token=token)


# ---------- привязка телефона: отправка ----------


async def test_link_send_phone_already_set_409(otp):
    user = _user(phone=PHONE, phone_verified=True)

    with pytest.raises(HTTPException) as exc:
        await svc.send_link_phone_code(_db(), MagicMock(), MagicMock(), user, "+79990000000")

    assert exc.value.status_code == 409
    assert exc.value.detail == "phone_already_set"
    otp.send.assert_not_called()


async def test_link_send_phone_taken_409_before_sms(otp, monkeypatch):
    monkeypatch.setattr(
        user_repo, "get_user_by_phone", AsyncMock(return_value=_user(phone=PHONE))
    )

    with pytest.raises(HTTPException) as exc:
        await svc.send_link_phone_code(_db(), MagicMock(), MagicMock(), _user(), PHONE)

    assert exc.value.detail == "phone_taken"
    otp.send.assert_not_called()  # SMS стоит денег — не шлём на занятый номер


async def test_link_send_uses_link_purpose_and_user_bound_subject(otp, monkeypatch):
    monkeypatch.setattr(user_repo, "get_user_by_phone", AsyncMock(return_value=None))
    user = _user()

    await svc.send_link_phone_code(_db(), "R", "SMS", user, PHONE)

    otp.send.assert_awaited_once_with(
        "R", "SMS", PHONE, purpose=OTPPurpose.link_phone, subject=f"{user.id}:{PHONE}"
    )


# ---------- привязка телефона: подтверждение ----------


@pytest.mark.parametrize(
    ("error", "detail"),
    [(OTPExpiredError, "code_expired"), (OTPInvalidError, "invalid_code")],
)
async def test_link_verify_bad_code_is_400_not_401(otp, side_effects, error, detail):
    # 401 под /users/me фронт принял бы за протухшую сессию и разлогинил.
    otp.consume.side_effect = error
    user = _user()

    with pytest.raises(HTTPException) as exc:
        await svc.verify_link_phone(_db(), "R", user, PHONE, CODE, ip=None, user_agent=None)

    assert exc.value.status_code == 400
    assert exc.value.detail == detail
    assert user.phone is None


async def test_link_verify_success_sets_verified_phone(otp, side_effects):
    user, db = _user(), _db()

    result = await svc.verify_link_phone(db, "R", user, PHONE, CODE, ip="1.1.1.1", user_agent="UA")

    assert result.phone == PHONE
    assert result.is_phone_verified is True
    otp.consume.assert_awaited_once_with(
        "R", CODE, purpose=OTPPurpose.link_phone, subject=f"{user.id}:{PHONE}"
    )
    side_effects.audit.assert_awaited_once()
    assert side_effects.audit.await_args.kwargs["action"] == "phone_linked"
    db.commit.assert_awaited_once()


async def test_link_verify_unique_race_409(otp, side_effects):
    db = _db()
    db.commit = AsyncMock(side_effect=IntegrityError("x", "y", Exception("dup")))

    with pytest.raises(HTTPException) as exc:
        await svc.verify_link_phone(db, "R", _user(), PHONE, CODE, ip=None, user_agent=None)

    assert exc.value.status_code == 409
    assert exc.value.detail == "phone_taken"
    db.rollback.assert_awaited_once()


# ---------- reauth ----------


async def test_reauth_without_phone_409(otp):
    with pytest.raises(HTTPException) as exc:
        await svc.send_reauth_code("R", "SMS", _user(email="a@b.c"))

    assert exc.value.detail == "phone_not_set"
    otp.send.assert_not_called()


async def test_reauth_sends_to_account_phone(otp):
    user = _user(phone=PHONE, phone_verified=True)

    await svc.send_reauth_code("R", "SMS", user)

    otp.send.assert_awaited_once_with(
        "R", "SMS", PHONE, purpose=OTPPurpose.reauth, subject=str(user.id)
    )


# ---------- подключение входа по почте ----------


async def test_email_login_when_email_exists_409(otp, side_effects):
    user = _user(phone=PHONE, phone_verified=True, email="old@b.c")

    with pytest.raises(HTTPException) as exc:
        await svc.request_email_login(
            _db(), "R", user, "new@b.c", "Password123", CODE, ip=None, user_agent=None
        )

    assert exc.value.detail == "email_already_set"
    otp.consume.assert_not_called()  # код не сжигаем зря


async def test_email_login_bad_code_400(otp, side_effects):
    otp.consume.side_effect = OTPInvalidError
    user = _user(phone=PHONE, phone_verified=True)

    with pytest.raises(HTTPException) as exc:
        await svc.request_email_login(
            _db(), "R", user, "new@b.c", "Password123", CODE, ip=None, user_agent=None
        )

    assert exc.value.status_code == 400
    assert user.hashed_password is None


async def test_email_login_taken_409(otp, side_effects, monkeypatch):
    monkeypatch.setattr(
        user_repo, "get_user_by_email", AsyncMock(return_value=_user(email="new@b.c"))
    )

    with pytest.raises(HTTPException) as exc:
        await svc.request_email_login(
            _db(), "R", _user(phone=PHONE, phone_verified=True),
            "new@b.c", "Password123", CODE, ip=None, user_agent=None,
        )

    assert exc.value.detail == "email_taken"


async def test_email_login_success_sets_password_and_pending(otp, side_effects, monkeypatch):
    monkeypatch.setattr(user_repo, "get_user_by_email", AsyncMock(return_value=None))
    user, db = _user(phone=PHONE, phone_verified=True), _db()

    await svc.request_email_login(
        db, "R", user, "new@b.c", "Password123", CODE, ip=None, user_agent=None
    )

    otp.consume.assert_awaited_once_with(
        "R", CODE, purpose=OTPPurpose.reauth, subject=str(user.id)
    )
    # Пароль задан, адрес ждёт подтверждения — email ещё не записан.
    assert user.hashed_password and user.hashed_password != "Password123"
    assert user.pending_email == "new@b.c"
    assert user.email is None
    # Ссылка уходит на НОВЫЙ адрес, токен — цели смены email.
    assert side_effects.email.await_args.kwargs["to_email"] == "new@b.c"
    assert side_effects.token.await_args.kwargs["purpose"] == "email_change"
    db.commit.assert_awaited_once()
