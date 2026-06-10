"""Security-инварианты сервиса аутентификации.

Все тесты мокают слой репозитория через monkeypatch, чтобы не зависеть
от живой PostgreSQL. Проверяют именно security-поведение, а не CRUD.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import ANY, AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.services import auth as auth_service
from app.models.user import EmailVerificationToken
from app.repositories import user as user_repo
from sqlalchemy.exc import IntegrityError


# bcrypt медленный (~250мс) — хешируем один раз на модуль.
_CACHED_PASSWORD_HASH = auth_service.hash_password("CorrectPass1")


def _fake_user(*, is_active: bool = True):
    user = MagicMock()
    user.id = uuid4()
    user.email = "alice@example.com"
    user.is_active = is_active
    user.hashed_password = _CACHED_PASSWORD_HASH
    user.roles = []
    return user


def _fake_session():
    session = MagicMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    return session


# ---------- register: no user enumeration ----------


async def test_register_existing_email_returns_none(monkeypatch):
    """Существующий email НЕ должен порождать исключение — это enumeration."""
    monkeypatch.setattr(
        user_repo, "get_user_by_email", AsyncMock(return_value=_fake_user())
    )
    create_user_mock = AsyncMock()
    monkeypatch.setattr(user_repo, "create_user", create_user_mock)

    result = await auth_service.register_user(
        _fake_session(), "alice@example.com", "CorrectPass1"
    )
    assert result is None
    create_user_mock.assert_not_called()


async def test_register_race_integrity_error_returns_none(monkeypatch):
    """UNIQUE-race на INSERT не должен пробрасываться как 500."""
    monkeypatch.setattr(
        user_repo, "get_user_by_email", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        user_repo,
        "create_user",
        AsyncMock(side_effect=IntegrityError("x", "y", Exception("dup"))),
    )
    session = _fake_session()

    result = await auth_service.register_user(
        session, "alice@example.com", "CorrectPass1"
    )
    assert result is None
    session.rollback.assert_awaited_once()


# ---------- login: timing-attack guard + invariants ----------


async def test_login_no_user_calls_dummy_verify(monkeypatch):
    """Отсутствие user должно проходить через bcrypt (dummy) — иначе timing leak."""
    monkeypatch.setattr(
        user_repo, "get_user_by_email", AsyncMock(return_value=None)
    )
    dummy_mock = MagicMock()
    monkeypatch.setattr(auth_service, "dummy_verify_password", dummy_mock)

    with pytest.raises(ValueError, match="invalid_credentials"):
        await auth_service.login_user(
            _fake_session(), "ghost@example.com", "whatever"
        )
    dummy_mock.assert_called_once()


async def test_login_blocked_user_rejected(monkeypatch):
    """is_active=False — токены не выдаются."""
    user = _fake_user(is_active=False)
    monkeypatch.setattr(
        user_repo, "get_user_by_email", AsyncMock(return_value=user)
    )

    with pytest.raises(ValueError, match="user_blocked"):
        await auth_service.login_user(
            _fake_session(), "alice@example.com", "CorrectPass1"
        )


# ---------- refresh: rotation + reuse detection ----------


def _fake_db_refresh_token(*, is_revoked: bool = False, expired: bool = False):
    tok = MagicMock()
    tok.user_id = uuid4()
    tok.is_revoked = is_revoked
    tok.expires_at = datetime.now(timezone.utc) + (
        timedelta(days=-1) if expired else timedelta(days=1)
    )
    return tok


async def test_refresh_rotation_issues_new_pair(monkeypatch):
    """Успешный refresh должен отозвать старый и создать новый refresh."""
    db_tok = _fake_db_refresh_token()
    user = _fake_user()
    user.id = db_tok.user_id

    monkeypatch.setattr(
        user_repo, "get_refresh_token_by_hash", AsyncMock(return_value=db_tok)
    )
    revoke_mock = AsyncMock(return_value=1)
    monkeypatch.setattr(user_repo, "revoke_refresh_token", revoke_mock)
    monkeypatch.setattr(
        user_repo, "get_user_by_id", AsyncMock(return_value=user)
    )
    create_mock = AsyncMock()
    monkeypatch.setattr(user_repo, "create_refresh_token", create_mock)

    result = await auth_service.refresh_access_token(_fake_session(), "raw-token")

    assert result.access_token and result.refresh_token
    assert result.refresh_token != "raw-token"
    revoke_mock.assert_awaited_once()
    create_mock.assert_awaited_once()


async def test_refresh_reuse_attack_revokes_all(monkeypatch):
    """Если revoke вернул 0, но токен в БД был — это reuse-attack.
    Все активные refresh юзера должны быть отозваны."""
    db_tok = _fake_db_refresh_token(is_revoked=True)
    monkeypatch.setattr(
        user_repo, "get_refresh_token_by_hash", AsyncMock(return_value=db_tok)
    )
    monkeypatch.setattr(
        user_repo, "revoke_refresh_token", AsyncMock(return_value=0)
    )
    revoke_all_mock = AsyncMock(return_value=2)
    monkeypatch.setattr(
        user_repo, "revoke_all_refresh_tokens_for_user", revoke_all_mock
    )

    with pytest.raises(ValueError, match="invalid_or_expired_token"):
        await auth_service.refresh_access_token(_fake_session(), "raw-token")
    # первый аргумент — сессия (ANY), второй — user_id отозванного токена
    revoke_all_mock.assert_awaited_once_with(ANY, db_tok.user_id)


async def test_refresh_expired_token_rejected(monkeypatch):
    """Истёкший refresh не должен попадать в логику revoke/rotation."""
    monkeypatch.setattr(
        user_repo,
        "get_refresh_token_by_hash",
        AsyncMock(return_value=_fake_db_refresh_token(expired=True)),
    )
    revoke_mock = AsyncMock()
    monkeypatch.setattr(user_repo, "revoke_refresh_token", revoke_mock)

    with pytest.raises(ValueError, match="invalid_or_expired_token"):
        await auth_service.refresh_access_token(_fake_session(), "raw-token")
    revoke_mock.assert_not_called()


# ---------- email verify + logout: atomicity ----------


async def test_verify_email_race_rejected(monkeypatch):
    """mark_email_token_used вернул 0 → токен уже использован параллельно."""
    db_tok = MagicMock()
    db_tok.user_id = uuid4()
    db_tok.expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    db_tok.used_at = None
    db_tok.purpose = EmailVerificationToken.PURPOSE_VERIFY
    monkeypatch.setattr(
        user_repo,
        "get_email_verification_token_by_hash",
        AsyncMock(return_value=db_tok),
    )
    monkeypatch.setattr(
        user_repo, "mark_email_token_used", AsyncMock(return_value=0)
    )

    with pytest.raises(ValueError, match="invalid_or_expired_token"):
        await auth_service.verify_email(_fake_session(), "raw-token")


# ---------- L2: разделение токенов verify / email_change ----------


async def test_verify_email_rejects_email_change_token(monkeypatch):
    """Токен смены email нельзя предъявить на /verify-email (аудит L2)."""
    db_tok = MagicMock()
    db_tok.user_id = uuid4()
    db_tok.expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    db_tok.used_at = None
    db_tok.purpose = EmailVerificationToken.PURPOSE_EMAIL_CHANGE
    monkeypatch.setattr(
        user_repo,
        "get_email_verification_token_by_hash",
        AsyncMock(return_value=db_tok),
    )
    mark = AsyncMock()
    monkeypatch.setattr(user_repo, "mark_email_token_used", mark)

    with pytest.raises(ValueError, match="invalid_or_expired_token"):
        await auth_service.verify_email(_fake_session(), "raw-token")
    mark.assert_not_called()  # отвергли по purpose до пометки использования


async def test_confirm_email_change_rejects_verify_token(monkeypatch):
    """Регистрационный токен нельзя предъявить на /confirm-email-change (L2)."""
    db_tok = MagicMock()
    db_tok.user_id = uuid4()
    db_tok.expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    db_tok.used_at = None
    db_tok.purpose = EmailVerificationToken.PURPOSE_VERIFY
    monkeypatch.setattr(
        user_repo,
        "get_email_verification_token_by_hash",
        AsyncMock(return_value=db_tok),
    )
    mark = AsyncMock()
    monkeypatch.setattr(user_repo, "mark_email_token_used", mark)

    with pytest.raises(HTTPException) as ei:
        await auth_service.confirm_email_change(
            _fake_session(), "raw-token", ip=None, user_agent=None
        )
    assert ei.value.status_code == 400
    mark.assert_not_called()


async def test_logout_unknown_token_rejected(monkeypatch):
    """revoke_refresh_token=0 — токен не найден или уже отозван → 401."""
    monkeypatch.setattr(
        user_repo, "revoke_refresh_token", AsyncMock(return_value=0)
    )

    with pytest.raises(ValueError, match="invalid_or_expired_token"):
        await auth_service.logout_user(_fake_session(), "raw-token")


# ---------- phone-only users: нет пароля — нет парольного входа ----------


async def test_login_phone_only_user_rejected(monkeypatch):
    """Юзер без hashed_password (вход по телефону) не логинится паролем."""
    user = _fake_user()
    user.hashed_password = None
    monkeypatch.setattr(
        user_repo, "get_user_by_email", AsyncMock(return_value=user)
    )

    with pytest.raises(ValueError, match="invalid_credentials"):
        await auth_service.login_user(
            _fake_session(), "alice@example.com", "CorrectPass1"
        )


async def test_change_password_phone_only_user_403(monkeypatch):
    user = _fake_user()
    user.hashed_password = None

    with pytest.raises(HTTPException) as exc:
        await auth_service.change_password(
            _fake_session(),
            user,
            "anything",
            "NewPass123",
            ip=None,
            user_agent=None,
        )
    assert exc.value.status_code == 403
