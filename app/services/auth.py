import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
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

logger = logging.getLogger(__name__)
# ИСПРАВЛЕНО: отдельный логгер для security-событий, чтобы можно было
# направлять его в SIEM/отдельный sink на стейдже 14. Никогда не пишет
# самих паролей/токенов — только email/user_id и тип события.
security_logger = logging.getLogger("app.security")


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

        raw_token, token_hash = generate_verification_token()
        expires_at = datetime.now(timezone.utc) + timedelta(hours=24)

        await user_repo.create_email_verification_token(
            db, user.id, token_hash, expires_at
        )

        # заглушка отправки почты
        logger.info("[DEV] Verify token for %s: %s", email, raw_token)
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
        or db_token.expires_at < datetime.now(timezone.utc)
        or db_token.used_at
    ):
        raise ValueError("Невалидный токен")

    # ИСПРАВЛЕНО: атомарное использование токена (race condition):
    # mark_email_token_used обновляет только если used_at IS NULL и
    # возвращает количество затронутых строк. Если 0 — токен уже
    # использован параллельным запросом.
    marked = await user_repo.mark_email_token_used(db, token_hash)
    if marked == 0:
        security_logger.warning(
            "email_verify_race user_id=%s", db_token.user_id
        )
        raise ValueError("Невалидный токен")

    user = await user_repo.get_user_by_id(db, db_token.user_id)

    if not user:
        await db.rollback()
        raise ValueError("Пользователь не найден")

    user.is_email_verified = True

    await db.commit()


async def login_user(db: AsyncSession, email: str, password: str) -> TokenResponse:
    user = await user_repo.get_user_by_email(db, email)

    # ИСПРАВЛЕНО: timing attack — раньше ответ "пользователь не найден"
    # был значительно быстрее ответа с реальным bcrypt-сравнением.
    # Теперь при отсутствии юзера выполняем dummy-верификацию и отдаём
    # тот же 401, что при неверном пароле.
    if not user:
        dummy_verify_password()
        security_logger.info("login_failed reason=no_user email=%s", email)
        raise ValueError("Неверный email или пароль")

    if not verify_password(password, user.hashed_password):
        security_logger.info("login_failed reason=bad_password user_id=%s", user.id)
        raise ValueError("Неверный email или пароль")

    # ИСПРАВЛЕНО: заблокированный пользователь не должен получать токены.
    if not user.is_active:
        security_logger.warning("login_blocked user_id=%s", user.id)
        raise ValueError("Пользователь заблокирован")

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
        raise ValueError("Невалидный токен")

    if db_token.expires_at < datetime.now(timezone.utc):
        raise ValueError("Невалидный токен")

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
        raise ValueError("Невалидный токен")

    user = await user_repo.get_user_by_id(db, db_token.user_id)
    if not user:
        await db.rollback()
        raise ValueError("Пользователь не найден")

    if not user.is_active:
        await db.rollback()
        raise ValueError("Пользователь заблокирован")

    roles = [r.role.value for r in user.roles]
    access = create_access_token(str(user.id), roles)

    raw_refresh = create_refresh_token_value()
    new_hash = hash_token(raw_refresh)
    expires_at = datetime.now(timezone.utc) + timedelta(
        days=settings.refresh_token_expire_days
    )
    await user_repo.create_refresh_token(db, user.id, new_hash, expires_at)

    await db.commit()
    return TokenResponse(
        access_token=access, refresh_token=raw_refresh, token_type="bearer"
    )


async def logout_user(db: AsyncSession, raw_refresh_token: str):
    token_hash = hash_token(raw_refresh_token)

    # ИСПРАВЛЕНО: один атомарный UPDATE вместо SELECT+UPDATE.
    # Возвращает 0, если токена нет или он уже отозван — пробрасываем
    # 401, чтобы не вводить пользователя в заблуждение об успехе.
    revoked = await user_repo.revoke_refresh_token(db, token_hash)
    if revoked == 0:
        raise ValueError("Refresh token не найден")
    await db.commit()
