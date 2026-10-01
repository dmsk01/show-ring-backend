"""Реестр способов аутентификации: телефон основной, email по флагам."""

from app.config import settings
from app.services.auth_methods import (
    EMAIL_PASSWORD,
    PHONE_OTP,
    enabled_auth_methods,
    primary_auth_method,
)


def _by_id():
    return {m.id: m for m in enabled_auth_methods()}


def test_defaults_phone_primary_email_login_only(monkeypatch):
    monkeypatch.setattr(settings, "auth_email_login_enabled", True)
    monkeypatch.setattr(settings, "auth_email_registration_enabled", False)

    methods = enabled_auth_methods()

    assert primary_auth_method() == PHONE_OTP
    assert methods[0].id == PHONE_OTP  # основной — первый
    assert methods[0].sign_in and methods[0].sign_up
    email = _by_id()[EMAIL_PASSWORD]
    assert email.sign_in is True
    assert email.sign_up is False


def test_email_fully_disabled_is_hidden(monkeypatch):
    monkeypatch.setattr(settings, "auth_email_login_enabled", False)
    monkeypatch.setattr(settings, "auth_email_registration_enabled", False)

    assert [m.id for m in enabled_auth_methods()] == [PHONE_OTP]


def test_phone_cannot_be_disabled_by_email_flags(monkeypatch):
    # Регистрация по телефону — единственный путь при закрытой email-регистрации.
    monkeypatch.setattr(settings, "auth_email_login_enabled", False)
    monkeypatch.setattr(settings, "auth_email_registration_enabled", False)

    phone = _by_id()[PHONE_OTP]
    assert phone.sign_in and phone.sign_up
