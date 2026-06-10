"""Валидация телефонных схем: E.164 и формат кода."""

import pytest
from pydantic import ValidationError

from app.schemas.user import PhoneSendCodeRequest, PhoneVerifyCodeRequest


@pytest.mark.parametrize(
    "phone",
    ["+79991234567", "+12025550123", "+442071838750"],
)
def test_valid_e164_accepted(phone):
    assert PhoneSendCodeRequest(phone=phone).phone == phone


def test_phone_is_stripped():
    assert PhoneSendCodeRequest(phone=" +79991234567 ").phone == "+79991234567"


@pytest.mark.parametrize(
    "phone",
    [
        "79991234567",       # без +
        "+0991234567",       # ведущий ноль после +
        "+7 999 123 45 67",  # пробелы внутри
        "+7999123",          # слишком короткий
        "+799912345678901234",  # длиннее 15 цифр
        "not-a-phone",
    ],
)
def test_invalid_e164_rejected(phone):
    with pytest.raises(ValidationError):
        PhoneSendCodeRequest(phone=phone)


def test_verify_code_format():
    req = PhoneVerifyCodeRequest(phone="+79991234567", code="123456")
    assert req.code == "123456"
    with pytest.raises(ValidationError):
        PhoneVerifyCodeRequest(phone="+79991234567", code="12ab56")
