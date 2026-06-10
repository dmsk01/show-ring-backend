import logging
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.user import EmailVerificationToken
from app.schemas.user import TokenResponse
from app.utils.security import (
    create_access_token,
    create_refresh_token_value,
    dummy_verify_password,
    hash_password,
    generate_verification_token,
    hash_token,
    verify_password,
)
from app.repositories import user as user_repo
from app.repositories import security_audit as audit_repo
from app.services.email_tasks import enqueue_transactional_email

logger = logging.getLogger(__name__)
# ИСПРАВЛЕНО: отдельный логгер для security-событий, чтобы можно было
# направлять его в SIEM/отдельный sink на стейдже 14. Никогда не пишет
# самих паролей/токенов — только email/user_id и тип события.
security_logger = logging.getLogger("app.security")


async def _issue_email_verification(db: AsyncSession, user) -> None:
    """
    Создать одноразовый verify-токен и поставить письмо подтверждения
    регистрации в очередь (этап 19). БЕЗ commit — коммитит вызывающий.
    Используется и в register_user, и в resend_verification.
    """
    raw_token, token_hash = generate_verification_token()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
    await user_repo.create_email_verification_token(
        db, user.id, token_hash, expires_at
    )
    confirm_url = (
        f"{settings.frontend_base_url}/verify-email?token={raw_token}"
    )
    await enqueue_transactional_email(
        db,
        user_id=user.id,
        to_email=user.email,
        template_name="verify_email",
        context={"confirm_url": confirm_url},
    )
    # В debug печатаем токен — удобно для dev-flow без работающего SMTP.
    if settings.debug:
        logger.info("[DEV] Verify token for %s: %s", user.email, raw_token)
    else:
        security_logger.info(
            "email_verification_requested user_id=%s", user.id
        )


async def resend_verification(db: AsyncSession, email: str) -> None:
    """
    Повторно отправить письмо подтверждения. Анти-enumeration: ответ
    роутера одинаков независимо от существования адреса и статуса.
    Письмо шлём только если юзер есть и email ещё не подтверждён.
    """
    user = await user_repo.get_user_by_email(db, email)
    if user is None:
        security_logger.info("resend_verification_no_user email=%s", email)
        return
    if user.is_email_verified:
        security_logger.info(
            "resend_verification_already_verified user_id=%s", user.id
        )
        return
    await _issue_email_verification(db, user)
    await db.commit()


async def register_user(db: AsyncSession, email: str, password: str):
    # ИСПРАВЛЕНО: убрана явная ошибка "Email уже занят" — раскрывала
    # факт регистрации (user enumeration). Теперь:
    # - если email свободен: создаём юзера + токен подтверждения;
    # - если email занят (поймали через IntegrityError от UNIQUE):
    #   откатываем транзакцию и тихо возвращаем None — роутер вернёт
    #   тот же ответ "проверьте email", что и для нового юзера.
    existing = await user_repo.get_user_by_email(db, email)
    if existing:
        security_logger.info("register_existing_email email=%s", email)
        return None

    hashed = hash_password(password)
    try:
        user = await user_repo.create_user(db, email, hashed)

        # Этап 19: TODO закрыт — письмо подтверждения реально уходит
        # через транзакционный канал (outbox → email_tasks). Токен —
        # sensitive: в prod не логируем (только факт внутри хелпера),
        # в debug печатаем для dev-flow без SMTP.
        await _issue_email_verification(db, user)
        await db.commit()
        return user
    except IntegrityError:
        # ИСПРАВЛЕНО: race condition между get_user_by_email и create_user
        # ловится через UNIQUE-constraint и возвращает то же поведение,
        # что и существующий email — без 500.
        await db.rollback()
        security_logger.info("register_race_collision email=%s", email)
        return None


async def verify_email(db: AsyncSession, raw_token: str):
    token_hash = hash_token(raw_token)

    db_token = await user_repo.get_email_verification_token_by_hash(db, token_hash)

    if (
        not db_token
        or db_token.purpose != EmailVerificationToken.PURPOSE_VERIFY
        or db_token.expires_at < datetime.now(timezone.utc)
        or db_token.used_at
    ):
        # Аудит L2: токен смены email (purpose != verify) на /verify-email
        # не принимаем — строгое разделение операций.
        raise ValueError("invalid_or_expired_token")

    # ИСПРАВЛЕНО: атомарное использование токена (race condition):
    # mark_email_token_used обновляет только если used_at IS NULL и
    # возвращает количество затронутых строк. Если 0 — токен уже
    # использован параллельным запросом.
    marked = await user_repo.mark_email_token_used(db, token_hash)
    if marked == 0:
        security_logger.warning(
            "email_verify_race user_id=%s", db_token.user_id
        )
        raise ValueError("invalid_or_expired_token")

    user = await user_repo.get_user_by_id(db, db_token.user_id)

    if not user:
        await db.rollback()
        raise ValueError("invalid_or_expired_token")

    user.is_email_verified = True

    await db.commit()


async def issue_token_pair(db: AsyncSession, user) -> TokenResponse:
    """
    Выдать пару access+refresh для уже аутентифицированного пользователя.
    Коммитит транзакцию. Вызывающий ОБЯЗАН проверить is_active до вызова.
    """
    roles = [r.role.value for r in user.roles]
    access = create_access_token(str(user.id), roles)

    raw_refresh = create_refresh_token_value()
    refresh_hash = hash_token(raw_refresh)
    expires_at = datetime.now(timezone.utc) + timedelta(
        days=settings.refresh_token_expire_days
    )
    await user_repo.create_refresh_token(db, user.id, refresh_hash, expires_at)
    await db.commit()

    return TokenResponse(
        access_token=access, refresh_token=raw_refresh, token_type="bearer"
    )


async def login_user(db: AsyncSession, email: str, password: str) -> TokenResponse:
    user = await user_repo.get_user_by_email(db, email)

    # ИСПРАВЛЕНО: timing attack — раньше ответ "пользователь не найден"
    # был значительно быстрее ответа с реальным bcrypt-сравнением.
    # Теперь при отсутствии юзера выполняем dummy-верификацию и отдаём
    # тот же 401, что при неверном пароле.
    if not user:
        dummy_verify_password()
        security_logger.info("login_failed reason=no_user email=%s", email)
        raise ValueError("invalid_credentials")

    # Phone-OTP: у телефонного пользователя пароля нет — парольный вход
    # для него закрыт. dummy-верификация выравнивает время ответа.
    if not user.hashed_password:
        dummy_verify_password()
        security_logger.info("login_failed reason=no_password user_id=%s", user.id)
        raise ValueError("invalid_credentials")

    if not verify_password(password, user.hashed_password):
        security_logger.info("login_failed reason=bad_password user_id=%s", user.id)
        raise ValueError("invalid_credentials")

    # ИСПРАВЛЕНО: заблокированный пользователь не должен получать токены.
    if not user.is_active:
        security_logger.warning("login_blocked user_id=%s", user.id)
        raise ValueError("user_blocked")

    # НАМЕРЕННО (зафиксировано review 2026-06-10): is_email_verified
    # здесь НЕ проверяется — вход с неподтверждённой почтой разрешён.
    # Продуктовый выбор: низкий барьер входа; верификация нужна для
    # доверия к адресу (письма, восстановление), а не как гейт логина.
    # Если решим ужесточить — блокировать чувствительные операции,
    # а не сам вход.

    return await issue_token_pair(db, user)


async def refresh_access_token(
    db: AsyncSession, raw_refresh_token: str
) -> TokenResponse:
    # Refresh token rotation + defense-in-depth от reuse-attack:
    #   1) ищем токен по хешу;
    #   2) пробуем атомарно отозвать (WHERE is_revoked=FALSE);
    #   3) если rowcount=0, но токен в БД существует — это reuse-attack
    #      (старый отозванный токен прислали повторно). Отзываем ВСЕ
    #      активные refresh этого юзера и заставляем войти заново;
    #   4) иначе выдаём новую пару access+refresh, старый недействителен.
    token_hash = hash_token(raw_refresh_token)
    db_token = await user_repo.get_refresh_token_by_hash(db, token_hash)

    if not db_token:
        raise ValueError("invalid_or_expired_token")

    if db_token.expires_at < datetime.now(timezone.utc):
        raise ValueError("invalid_or_expired_token")

    revoked = await user_repo.revoke_refresh_token(db, token_hash)
    if revoked == 0:
        # ИСПРАВЛЕНО (defense-in-depth): токен уже отозван, но повторно
        # предъявлен → reuse-attack. Аннулируем всю refresh-цепочку юзера.
        await user_repo.revoke_all_refresh_tokens_for_user(db, db_token.user_id)
        await db.commit()
        security_logger.warning(
            "refresh_token_reuse user_id=%s — all refresh tokens revoked",
            db_token.user_id,
        )
        raise ValueError("invalid_or_expired_token")

    user = await user_repo.get_user_by_id(db, db_token.user_id)
    if not user:
        await db.rollback()
        raise ValueError("invalid_or_expired_token")

    if not user.is_active:
        await db.rollback()
        raise ValueError("user_blocked")

    return await issue_token_pair(db, user)


async def logout_user(db: AsyncSession, raw_refresh_token: str):
    token_hash = hash_token(raw_refresh_token)

    # ИСПРАВЛЕНО: один атомарный UPDATE вместо SELECT+UPDATE.
    # Возвращает 0, если токена нет или он уже отозван — пробрасываем
    # 401, чтобы не вводить пользователя в заблуждение об успехе.
    revoked = await user_repo.revoke_refresh_token(db, token_hash)
    if revoked == 0:
        raise ValueError("invalid_or_expired_token")
    await db.commit()


# ---------------------------------------------------------------------
# Этап 19: смена email (через pending_email) и смена пароля.
#
# Эти функции HTTP-осведомлённы (raise HTTPException с машиночитаемым
# detail) — сознательное отступление от ValueError-паттерна выше: коды
# тут нюансные (403/409/400), а роутер всё равно прокидывает ip/UA/db.
# ---------------------------------------------------------------------


async def request_email_change(
    db: AsyncSession,
    user,
    new_email: str,
    current_password: str | None,
    *,
    ip: str | None,
    user_agent: str | None,
) -> None:
    """
    Запросить смену email. НЕ меняет users.email — пишет new_email в
    users.pending_email и шлёт письмо-подтверждение на новый адрес.
    Реальная смена — в confirm_email_change по клику. Старый адрес
    остаётся рабочим. Коммитит транзакцию сам.
    """
    # 1. Re-auth: без текущего пароля смену не запустить (украденный
    #    access-токен без пароля бессилен).
    if (
        not user.hashed_password
        or not current_password
        or not verify_password(current_password, user.hashed_password)
    ):
        security_logger.warning(
            "email_change_bad_password user_id=%s", user.id
        )
        raise HTTPException(status_code=403, detail="current_password_invalid")

    # 2. Адрес занят другим аккаунтом? (мягкая проверка до письма —
    #    финальная гарантия даёт UNIQUE при confirm).
    existing = await user_repo.get_user_by_email(db, new_email)
    if existing is not None and existing.id != user.id:
        raise HTTPException(status_code=409, detail="email_taken")

    # 3. pending_email + одноразовый токен (TTL 24ч) в общей таблице
    #    email_verification_tokens.
    user.pending_email = new_email
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
        to_email=new_email,
        template_name="email_change_confirm",
        context={"new_email": new_email, "confirm_url": confirm_url},
    )
    # Уведомляем и СТАРЫЙ адрес (review 2026-06-10): атакующий с
    # украденным паролем иначе тихо перевешивал аккаунт на свою почту —
    # письмо на новый адрес владельцу-жертве ничего не скажет (а отзыв
    # refresh-токенов при confirm ему не мешает: пароль у него есть).
    # Стандартная практика: «если это были не вы — смените пароль».
    await enqueue_transactional_email(
        db,
        user_id=user.id,
        to_email=user.email,
        template_name="email_change_notice",
        context={"new_email": new_email},
    )
    await audit_repo.record_security_event(
        db,
        user_id=user.id,
        action="email_change_requested",
        ip=ip,
        user_agent=user_agent,
        extra={"old_email": user.email, "new_email": new_email},
    )
    security_logger.info(
        "email_change_requested user_id=%s new=%s", user.id, new_email
    )
    if settings.debug:
        logger.info("[DEV] Email-change token for %s: %s", new_email, raw_token)
    await db.commit()


async def confirm_email_change(
    db: AsyncSession,
    raw_token: str,
    *,
    ip: str | None,
    user_agent: str | None,
) -> None:
    """
    Подтвердить смену email по токену из письма: pending_email → email,
    is_email_verified=True, отзыв всех refresh-токенов. Коммитит сам.
    """
    token_hash = hash_token(raw_token)
    db_token = await user_repo.get_email_verification_token_by_hash(
        db, token_hash
    )
    if (
        not db_token
        or db_token.purpose != EmailVerificationToken.PURPOSE_EMAIL_CHANGE
        or db_token.expires_at < datetime.now(timezone.utc)
        or db_token.used_at
    ):
        # Аудит L2: регистрационный токен (purpose != email_change) на
        # /confirm-email-change не принимаем.
        raise HTTPException(status_code=400, detail="invalid_or_expired_token")

    # Атомарно гасим токен (rowcount=0 → уже использован параллельно).
    marked = await user_repo.mark_email_token_used(db, token_hash)
    if marked == 0:
        raise HTTPException(status_code=400, detail="invalid_or_expired_token")

    user = await user_repo.get_user_by_id(db, db_token.user_id)
    if not user or not user.pending_email:
        # Токен не от смены email (например, регистрационный) либо
        # pending уже снят. Не наша операция.
        await db.rollback()
        raise HTTPException(status_code=400, detail="invalid_or_expired_token")

    old_email = user.email
    new_email = user.pending_email
    user.email = new_email
    user.pending_email = None
    user.is_email_verified = True
    # Отзыв сессий — в момент подтверждения, не запроса (иначе чужой
    # запрос смены разлогинивал бы законного владельца — DoS).
    await user_repo.revoke_all_refresh_tokens_for_user(db, user.id)
    await audit_repo.record_security_event(
        db,
        user_id=user.id,
        action="email_change_confirmed",
        ip=ip,
        user_agent=user_agent,
        extra={"old_email": old_email, "new_email": new_email},
    )
    try:
        await db.commit()
    except IntegrityError:
        # Новый адрес заняли между запросом и подтверждением (UNIQUE).
        await db.rollback()
        raise HTTPException(status_code=409, detail="email_taken")
    security_logger.info(
        "email_change_confirmed user_id=%s new=%s", user.id, new_email
    )


async def change_password(
    db: AsyncSession,
    user,
    current_password: str,
    new_password: str,
    *,
    ip: str | None,
    user_agent: str | None,
) -> None:
    """
    Сменить пароль: re-auth, хеширование нового, отзыв всех refresh,
    письмо-уведомление на текущий адрес, аудит. Коммитит сам.
    """
    if not user.hashed_password or not verify_password(current_password, user.hashed_password):
        security_logger.warning(
            "password_change_bad_password user_id=%s", user.id
        )
        raise HTTPException(status_code=403, detail="current_password_invalid")
    if verify_password(new_password, user.hashed_password):
        raise HTTPException(
            status_code=400, detail="password_same_as_current"
        )

    user.hashed_password = hash_password(new_password)
    await user_repo.revoke_all_refresh_tokens_for_user(db, user.id)
    await enqueue_transactional_email(
        db,
        user_id=user.id,
        to_email=user.email,
        template_name="password_changed",
        context={},
    )
    await audit_repo.record_security_event(
        db,
        user_id=user.id,
        action="password_changed",
        ip=ip,
        user_agent=user_agent,
    )
    await db.commit()
    security_logger.info("password_changed user_id=%s", user.id)
