"""
Бизнес-логика входа по телефону с OTP-кодом.

Состояние OTP живёт в Redis (TTL делает коды самоистекающими):
  otp:cooldown:{phone} — маркер «SMS уже отправлено» (SET NX EX, атомарно)
  otp:code:{phone}     — SHA-256 кода (не сам код), TTL = otp_code_ttl_seconds
  otp:attempts:{phone} — счётчик попыток ввода (INCR атомарен)
  otp:daily:{phone}    — суточный счётчик отправок (анти SMS-pumping)
"""

import logging
import secrets

from redis.asyncio import Redis
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.repositories import user as user_repo
from app.schemas.user import TokenResponse
from app.services.auth import issue_token_pair
from app.services.sms import SMSProvider
from app.utils.security import hash_token

logger = logging.getLogger(__name__)
security_logger = logging.getLogger("app.security")


class OTPRateLimitedError(Exception):
    """Повторная отправка раньше cooldown / суточный потолок. → 429"""


class OTPExpiredError(Exception):
    """Кода нет: истёк, не запрашивался или сожжён попытками. → 401"""


class OTPInvalidError(Exception):
    """Код неверный, попытки ещё остались. → 400"""


class OTPUserBlockedError(Exception):
    """Код верный, но пользователь заблокирован (is_active=False). → 401"""


def _cooldown_key(phone: str) -> str:
    return f"otp:cooldown:{phone}"


def _code_key(phone: str) -> str:
    return f"otp:code:{phone}"


def _attempts_key(phone: str) -> str:
    return f"otp:attempts:{phone}"


def _daily_key(phone: str) -> str:
    return f"otp:daily:{phone}"


def _generate_code() -> str:
    # secrets (не random): криптографический RNG. Ведущие нули сохраняем
    # форматированием — код всегда фиксированной длины.
    n = settings.otp_code_length
    return f"{secrets.randbelow(10 ** n):0{n}d}"


async def send_otp_code(redis: Redis, sms: SMSProvider, phone: str) -> None:
    # 1. Cooldown: SET NX EX атомарен — из двух параллельных запросов
    #    SMS отправит ровно один.
    ok = await redis.set(
        _cooldown_key(phone),
        "1",
        nx=True,
        ex=settings.otp_send_cooldown_seconds,
    )
    if not ok:
        security_logger.info("otp_send_cooldown phone=%s", phone)
        raise OTPRateLimitedError

    # 2. Суточный потолок на номер. INCR атомарен; expire ставим только
    #    первому инкременту — окно скользит от первой отправки.
    daily = await redis.incr(_daily_key(phone))
    if daily == 1:
        await redis.expire(_daily_key(phone), 86400)
    if daily > settings.otp_daily_limit:
        security_logger.warning("otp_daily_limit phone=%s", phone)
        raise OTPRateLimitedError

    # 3. Новый код перезаписывает старый (валиден только последний),
    #    счётчик попыток обнуляется.
    code = _generate_code()
    await redis.set(
        _code_key(phone), hash_token(code), ex=settings.otp_code_ttl_seconds
    )
    await redis.delete(_attempts_key(phone))

    # 4. Отправка. Сбой провайдера пробрасывается (роутер → 502);
    #    cooldown при этом остаётся — клиент не должен долбить ретраями.
    await sms.send(phone, f"Ваш код входа: {code}")

    if settings.debug:
        # Dev-flow без SMS-шлюза: код в логе. В проде — никогда.
        logger.info("[DEV] OTP for %s: %s", phone, code)
    else:
        security_logger.info("otp_sent phone=%s", phone)


async def verify_otp_code(
    db: AsyncSession, redis: Redis, phone: str, code: str
) -> TokenResponse:
    stored_hash = await redis.get(_code_key(phone))
    if stored_hash is None:
        security_logger.info("otp_verify_no_code phone=%s", phone)
        raise OTPExpiredError

    # Попытка регистрируется ДО сравнения: INCR атомарен, параллельные
    # запросы не получают «бесплатных» попыток.
    attempts = await redis.incr(_attempts_key(phone))
    if attempts == 1:
        # Счётчик живёт не дольше кода — иначе «висячие» попытки
        # блокировали бы СЛЕДУЮЩИЙ код (его счётчик чистит send).
        await redis.expire(_attempts_key(phone), settings.otp_code_ttl_seconds)
    if attempts > settings.otp_max_attempts:
        await redis.delete(_code_key(phone), _attempts_key(phone))
        security_logger.warning("otp_brute_force phone=%s", phone)
        raise OTPExpiredError

    # compare_digest: сравнение за константное время (timing attack).
    if not secrets.compare_digest(hash_token(code), stored_hash):
        if attempts >= settings.otp_max_attempts:
            # Последняя попытка истрачена — сжигаем код сразу.
            await redis.delete(_code_key(phone), _attempts_key(phone))
            security_logger.warning("otp_attempts_exhausted phone=%s", phone)
        else:
            security_logger.info(
                "otp_wrong_code phone=%s attempt=%s", phone, attempts
            )
        raise OTPInvalidError

    # Успех: код строго одноразовый.
    await redis.delete(_code_key(phone), _attempts_key(phone))

    # Find-or-create: подтверждённый номер = аутентифицированный
    # пользователь; отдельного шага «регистрация» нет.
    user = await user_repo.get_user_by_phone(db, phone)
    if user is None:
        try:
            user = await user_repo.create_user_by_phone(db, phone)
            security_logger.info("otp_user_created user_id=%s", user.id)
        except IntegrityError:
            # Race двух параллельных verify: UNIQUE(phone) пропустил
            # одного, второй читает созданного.
            await db.rollback()
            user = await user_repo.get_user_by_phone(db, phone)
            if user is None:
                raise OTPExpiredError

    if not user.is_active:
        security_logger.warning("otp_login_blocked user_id=%s", user.id)
        raise OTPUserBlockedError

    security_logger.info("otp_login_success user_id=%s", user.id)
    return await issue_token_pair(db, user)
