"""
Способы входа авторизованного пользователя: привязка телефона и
подключение входа по почте (docs/superpowers/specs/2026-10-01-phone-primary-auth-design.md).

- Legacy-пользователь (зарегистрирован по email до перехода на телефон)
  привязывает номер: код уходит на НОВЫЙ номер (OTP-цель link_phone).
- Телефонный пользователь подключает вход по почте: вместо текущего
  пароля (его нет) — свежий код на номер аккаунта (OTP-цель reauth).

Как и смена email/пароля в services/auth.py, функции HTTP-осведомлённы:
коды тут нюансные (400/409), а роутер всё равно прокидывает ip/UA.

ВАЖНО: ошибки кода здесь — 400, а не 401 (как на /auth/verify-code).
Эндпоинты живут под /users/me, и фронтовый axios-интерсептор трактует
401 вне /auth/ как протухшую сессию: refresh + редирект на логин —
пользователь вылетел бы из аккаунта за опечатку в коде.
"""

import logging
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.user import EmailVerificationToken, User
from app.repositories import security_audit as audit_repo
from app.repositories import user as user_repo
from app.services.email_tasks import enqueue_transactional_email
from app.services.otp_auth import (
    OTPExpiredError,
    OTPInvalidError,
    OTPPurpose,
    consume_otp_code,
    send_otp_code,
)
from app.services.sms import SMSProvider
from app.utils.security import generate_verification_token, hash_password

logger = logging.getLogger(__name__)
security_logger = logging.getLogger("app.security")


def _link_subject(user: User, phone: str) -> str:
    # Код привязки годится только этому пользователю и только к номеру,
    # на который отправлен: два юзера, привязывающие один номер, не
    # перетирают коды друг друга.
    return f"{user.id}:{phone}"


async def _consume_or_400(
    redis: Redis, code: str, *, purpose: OTPPurpose, subject: str
) -> None:
    try:
        await consume_otp_code(redis, code, purpose=purpose, subject=subject)
    except OTPExpiredError:
        raise HTTPException(status_code=400, detail="code_expired")
    except OTPInvalidError:
        raise HTTPException(status_code=400, detail="invalid_code")


def _ensure_can_link_phone(user: User) -> None:
    # Смена уже подтверждённого номера — отдельная задача (нужен код и на
    # старый номер, иначе украденная сессия перевешивает аккаунт на чужую
    # SIM). Пока — через поддержку.
    if user.phone and user.is_phone_verified:
        raise HTTPException(status_code=409, detail="phone_already_set")


# ---------------------------------------------------------------------
# Привязка телефона
# ---------------------------------------------------------------------


async def send_link_phone_code(
    db: AsyncSession, redis: Redis, sms: SMSProvider, user: User, phone: str
) -> None:
    """Отправить код привязки на новый номер. OTP-ошибки (rate limit,
    сбой SMS) пробрасываются — роутер маппит их как /auth/send-code."""
    _ensure_can_link_phone(user)
    # Занятость проверяем ДО отправки: SMS стоит денег. Эндпоинт под
    # авторизацией и rate-limit — перебор номеров через него ограничен.
    owner = await user_repo.get_user_by_phone(db, phone)
    if owner is not None and owner.id != user.id:
        raise HTTPException(status_code=409, detail="phone_taken")
    await send_otp_code(
        redis,
        sms,
        phone,
        purpose=OTPPurpose.link_phone,
        subject=_link_subject(user, phone),
    )


async def verify_link_phone(
    db: AsyncSession,
    redis: Redis,
    user: User,
    phone: str,
    code: str,
    *,
    ip: str | None,
    user_agent: str | None,
) -> User:
    """Проверить код и записать номер как подтверждённый. Коммитит сам."""
    _ensure_can_link_phone(user)
    await _consume_or_400(
        redis,
        code,
        purpose=OTPPurpose.link_phone,
        subject=_link_subject(user, phone),
    )

    user.phone = phone
    user.is_phone_verified = True
    await audit_repo.record_security_event(
        db,
        user_id=user.id,
        action="phone_linked",
        ip=ip,
        user_agent=user_agent,
        extra={"phone": phone},
    )
    try:
        await db.commit()
    except IntegrityError:
        # Номер заняли между отправкой кода и подтверждением (UNIQUE).
        await db.rollback()
        raise HTTPException(status_code=409, detail="phone_taken")
    security_logger.info("phone_linked user_id=%s", user.id)
    return user


# ---------------------------------------------------------------------
# Подключение входа по почте
# ---------------------------------------------------------------------


async def send_reauth_code(
    redis: Redis, sms: SMSProvider, user: User
) -> None:
    """Код подтверждения действия на номер аккаунта."""
    if not user.phone or not user.is_phone_verified:
        raise HTTPException(status_code=409, detail="phone_not_set")
    await send_otp_code(
        redis,
        sms,
        user.phone,
        purpose=OTPPurpose.reauth,
        subject=str(user.id),
    )


async def request_email_login(
    db: AsyncSession,
    redis: Redis,
    user: User,
    email: str,
    password: str,
    code: str,
    *,
    ip: str | None,
    user_agent: str | None,
) -> None:
    """
    Подключить вход по почте к аккаунту без email. Пароль ставится сразу,
    адрес — в pending_email; вход по почте заработает после клика по
    ссылке (POST /auth/confirm-email-change переносит pending → email).

    Пока адрес не подтверждён (email IS NULL), повторный запрос разрешён и
    перезаписывает пароль и pending_email — так исправляется опечатка.
    Коммитит сам.
    """
    if user.email:
        # Почта уже есть — для смены есть PUT /users/me (с паролем).
        raise HTTPException(status_code=409, detail="email_already_set")

    # Re-auth свежим кодом: украденная access-кука без телефона не позволит
    # повесить на аккаунт чужую почту и пароль.
    await _consume_or_400(
        redis, code, purpose=OTPPurpose.reauth, subject=str(user.id)
    )

    existing = await user_repo.get_user_by_email(db, email)
    if existing is not None and existing.id != user.id:
        raise HTTPException(status_code=409, detail="email_taken")

    user.hashed_password = hash_password(password)
    user.pending_email = email
    raw_token, token_hash = generate_verification_token()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
    await user_repo.create_email_verification_token(
        db,
        user.id,
        token_hash,
        expires_at,
        purpose=EmailVerificationToken.PURPOSE_EMAIL_CHANGE,
    )
    confirm_url = (
        f"{settings.frontend_base_url}/confirm-email-change?token={raw_token}"
    )
    await enqueue_transactional_email(
        db,
        user_id=user.id,
        to_email=email,
        template_name="email_change_confirm",
        context={"new_email": email, "confirm_url": confirm_url},
    )
    await audit_repo.record_security_event(
        db,
        user_id=user.id,
        action="email_login_requested",
        ip=ip,
        user_agent=user_agent,
        extra={"new_email": email},
    )
    if settings.debug:
        logger.info("[DEV] Email-login token for %s: %s", email, raw_token)
    await db.commit()
    security_logger.info("email_login_requested user_id=%s", user.id)
