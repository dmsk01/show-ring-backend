"""SMTP-параметры отправки: прод-конфигурация (587 + STARTTLS) должна работать."""

import aiosmtplib
import pytest

from app.config import Settings, settings
from app.services import email as email_module

_DB = "postgresql+asyncpg://u:p@localhost:5432/db"
_STRONG = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


@pytest.fixture
def sent(monkeypatch):
    calls: list[dict] = []

    async def fake_send(message, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(aiosmtplib, "send", fake_send)
    return calls


async def test_starttls_is_negotiated_when_implicit_tls_off(monkeypatch, sent):
    # Порт 587: соединение открытым текстом, затем STARTTLS, если сервер
    # его объявляет. start_tls=False запрещал апгрейд — пароль SMTP и
    # письма шли бы в открытом виде (или сервер отверг бы AUTH).
    monkeypatch.setattr(settings, "smtp_use_tls", False)
    await email_module.send_email(
        to_email="u@example.com", subject="s", html_body="<p>b</p>", text_body="b"
    )
    assert sent[0]["use_tls"] is False
    assert sent[0]["start_tls"] is None  # авто: апгрейд, если поддерживается


async def test_implicit_tls_disables_starttls(monkeypatch, sent):
    # Порт 465: TLS с первого байта; STARTTLS поверх него aiosmtplib запрещает.
    monkeypatch.setattr(settings, "smtp_use_tls", True)
    await email_module.send_email(
        to_email="u@example.com", subject="s", html_body="<p>b</p>", text_body="b"
    )
    assert sent[0]["use_tls"] is True
    assert sent[0]["start_tls"] is False


def test_empty_optional_secrets_become_none():
    # docker-compose передаёт незаданные переменные как "" (${VAR:-}).
    # Пустой SMTP_USERNAME заставил бы aiosmtplib логиниться пустыми данными.
    s = Settings(
        database_url=_DB,
        secret_key=_STRONG,
        smtp_username="",
        smtp_password="",
        sms_api_key="",
        internal_api_key="",
    )
    assert s.smtp_username is None
    assert s.smtp_password is None
    assert s.sms_api_key is None
    assert s.internal_api_key is None
