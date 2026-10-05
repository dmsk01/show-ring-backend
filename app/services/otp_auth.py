"""
Бизнес-логика OTP-кодов по SMS: вход/регистрация по телефону, а также
подтверждение чувствительных действий и привязка номера.

Код выдаётся под конкретную цель (OTPPurpose) и субъект — код одной цели
не принимается в другой. Субъект — то, к чему привязан код:
  login      → номер телефона (вход/регистрация);
  reauth     → user_id (код на номер аккаунта, замена текущего пароля);
  link_phone → "{user_id}:{phone}" (код подходит только этому юзеру и
               только к номеру, на который отправлен).

Состояние живёт в Redis (TTL делает коды самоистекающими):
  otp:{purpose}:cooldown:{phone}   — маркер «SMS уже отправлено» (SET NX EX)
  otp:{purpose}:code:{subject}     — SHA-256 кода, TTL = otp_code_ttl_seconds
  otp:{purpose}:attempts:{subject} — счётчик попыток ввода (INCR атомарен)
  otp:daily:{phone}                — суточный счётчик отправок на номер,
                                     ОБЩИЙ для всех целей (анти SMS-pumping)
  otp:budget:{YYYY-MM-DD}          — счётчик SMS на весь сервис за сутки (UTC)
"""

import enum
from datetime import datetime, timezone
import logging
import secrets

from redis.asyncio import Redis
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.repositories import user as user_repo
from app.schemas.user import TokenResponse
from app.services import consent as consent_svc
from app.services import security_metrics
from app.services.auth import issue_token_pair
from app.services.sms import SMSProvider
from app.utils.security import hash_token

logger = logging.getLogger(__name__)
security_logger = logging.getLogger("app.security")


class OTPPurpose(str, enum.Enum):
    login = "login"
    reauth = "reauth"
    link_phone = "link_phone"


_SMS_TEXT = {
    OTPPurpose.login: "Ваш код входа: {code}",
    OTPPurpose.reauth: "Код подтверждения: {code}",
    OTPPurpose.link_phone: "Код для привязки номера: {code}",
}


class OTPRateLimitedError(Exception):
    """Повторная отправка раньше cooldown / суточный потолок. → 429"""


class OTPCountryNotAllowedError(Exception):
    """Номер вне белого списка стран (sms_allowed_phone_prefixes). → 400"""


class SMSBudgetExceededError(Exception):
    """Исчерпан суточный бюджет SMS на весь сервис. → 503"""


class OTPExpiredError(Exception):
    """Кода нет: истёк, не запрашивался или сожжён попытками. → 401"""


class OTPInvalidError(Exception):
    """Код неверный, попытки ещё остались. → 400"""


class OTPUserBlockedError(Exception):
    """Код верный, но пользователь заблокирован (is_active=False). → 401"""


def _cooldown_key(purpose: OTPPurpose, phone: str) -> str:
    # Cooldown на (цель, номер): только что вошедший по SMS пользователь
    # может сразу запросить код подтверждения действия, не ловя 429.
    return f"otp:{purpose.value}:cooldown:{phone}"


def _code_key(purpose: OTPPurpose, subject: str) -> str:
    return f"otp:{purpose.value}:code:{subject}"


def _attempts_key(purpose: OTPPurpose, subject: str) -> str:
    return f"otp:{purpose.value}:attempts:{subject}"


def _daily_key(phone: str) -> str:
    return f"otp:daily:{phone}"


def _budget_key() -> str:
    return f"otp:budget:{datetime.now(timezone.utc):%Y-%m-%d}"


# Доли бюджета, на которых пишем предупреждение (сигнал для оповещения).
_BUDGET_ALERT_SHARES = (0.5, 0.8, 1.0)


def _phone_allowed(phone: str) -> bool:
    prefixes = settings.sms_allowed_phone_prefixes
    return not prefixes or any(phone.startswith(p) for p in prefixes)


async def _spend_budget(redis: Redis) -> None:
    """Учесть одно SMS в суточном бюджете сервиса; сверх него — отказ."""
    budget = settings.sms_daily_budget
    if budget <= 0:
        return
    key = _budget_key()
    used = await redis.incr(key)
    if used == 1:
        # Двое суток — ключ гарантированно переживает смену даты.
        await redis.expire(key, 2 * 86400)
    for share in _BUDGET_ALERT_SHARES:
        if used == max(1, int(budget * share)):
            security_logger.warning(
                "sms_budget_threshold share=%.0f%% used=%s budget=%s",
                share * 100,
                used,
                budget,
            )
    if used > budget:
        raise SMSBudgetExceededError


def _generate_code() -> str:
    # secrets (не random): криптографический RNG. Ведущие нули сохраняем
    # форматированием — код всегда фиксированной длины.
    n = settings.otp_code_length
    return f"{secrets.randbelow(10 ** n):0{n}d}"


async def send_otp_code(
    redis: Redis,
    sms: SMSProvider,
    phone: str,
    *,
    purpose: OTPPurpose = OTPPurpose.login,
    subject: str | None = None,
) -> None:
    """Сгенерировать код цели purpose для subject и отправить SMS на phone.

    subject по умолчанию — сам номер (цель login).
    """
    subject = subject or phone

    # 0. Белый список стран — до всех счётчиков: чужой номер ничего не тратит.
    if not _phone_allowed(phone):
        security_logger.warning("otp_country_blocked phone=%s", phone)
        raise OTPCountryNotAllowedError

    # 1. Cooldown: SET NX EX атомарен — из двух параллельных запросов
    #    SMS отправит ровно один.
    ok = await redis.set(
        _cooldown_key(purpose, phone),
        "1",
        nx=True,
        ex=settings.otp_send_cooldown_seconds,
    )
    if not ok:
        security_logger.info(
            "otp_send_cooldown purpose=%s phone=%s", purpose.value, phone
        )
        raise OTPRateLimitedError

    # 2. Суточный потолок на номер (все цели). INCR атомарен; expire ставим
    #    только первому инкременту — окно скользит от первой отправки.
    daily = await redis.incr(_daily_key(phone))
    if daily == 1:
        await redis.expire(_daily_key(phone), 86400)
    if daily > settings.otp_daily_limit:
        security_logger.warning("otp_daily_limit phone=%s", phone)
        raise OTPRateLimitedError

    # 2a. Общий бюджет SMS на сервис: последний рубеж, если накрутка идёт
    #     по множеству номеров с множества IP.
    try:
        await _spend_budget(redis)
    except SMSBudgetExceededError:
        security_logger.error("sms_budget_exceeded phone=%s", phone)
        raise

    # 3. Новый код перезаписывает старый (валиден только последний),
    #    счётчик попыток обнуляется.
    code = _generate_code()
    await redis.set(
        _code_key(purpose, subject),
        hash_token(code),
        ex=settings.otp_code_ttl_seconds,
    )
    await redis.delete(_attempts_key(purpose, subject))

    # 4. Отправка. Сбой провайдера пробрасывается (роутер → 502);
    #    cooldown при этом остаётся — клиент не должен долбить ретраями.
    await sms.send(phone, _SMS_TEXT[purpose].format(code=code))
    await security_metrics.record(security_metrics.SMS_SENT, redis=redis)

    if settings.debug:
        # Dev-flow без SMS-шлюза: код в логе. В проде — никогда.
        logger.info("[DEV] OTP %s for %s: %s", purpose.value, phone, code)
    else:
        security_logger.info(
            "otp_sent purpose=%s phone=%s", purpose.value, phone
        )


async def consume_otp_code(
    redis: Redis, code: str, *, purpose: OTPPurpose, subject: str
) -> None:
    """Проверить код и сжечь его (одноразовый). Бросает OTPExpiredError /
    OTPInvalidError. БД не трогает и ничего не коммитит."""
    code_key = _code_key(purpose, subject)
    attempts_key = _attempts_key(purpose, subject)

    stored_hash = await redis.get(code_key)
    if stored_hash is None:
        security_logger.info(
            "otp_verify_no_code purpose=%s subject=%s", purpose.value, subject
        )
        raise OTPExpiredError

    # Попытка регистрируется ДО сравнения: INCR атомарен, параллельные
    # запросы не получают «бесплатных» попыток.
    attempts = await redis.incr(attempts_key)
    if attempts == 1:
        # Счётчик живёт не дольше кода — иначе «висячие» попытки
        # блокировали бы СЛЕДУЮЩИЙ код (его счётчик чистит send).
        await redis.expire(attempts_key, settings.otp_code_ttl_seconds)
    if attempts > settings.otp_max_attempts:
        await redis.delete(code_key, attempts_key)
        security_logger.warning(
            "otp_brute_force purpose=%s subject=%s", purpose.value, subject
        )
        raise OTPExpiredError

    # Клиент Redis может вернуть bytes (без decode_responses) — compare_digest
    # не сравнивает str с bytes и бросил бы TypeError.
    if isinstance(stored_hash, bytes):
        stored_hash = stored_hash.decode()
    # compare_digest: сравнение за константное время (timing attack).
    if not secrets.compare_digest(hash_token(code), stored_hash):
        if attempts >= settings.otp_max_attempts:
            # Последняя попытка истрачена — сжигаем код сразу.
            await redis.delete(code_key, attempts_key)
            security_logger.warning(
                "otp_attempts_exhausted purpose=%s subject=%s",
                purpose.value,
                subject,
            )
        else:
            security_logger.info(
                "otp_wrong_code purpose=%s subject=%s attempt=%s",
                purpose.value,
                subject,
                attempts,
            )
        raise OTPInvalidError

    # Успех: код строго одноразовый. DEL атомарен и возвращает число
    # удалённых ключей — из двух параллельных верных запросов код
    # «съест» ровно один, второй получит OTPExpiredError.
    consumed = await redis.delete(code_key)
    await redis.delete(attempts_key)
    if consumed == 0:
        security_logger.warning(
            "otp_verify_race purpose=%s subject=%s", purpose.value, subject
        )
        raise OTPExpiredError
    await security_metrics.record(security_metrics.OTP_VERIFIED, redis=redis)


async def verify_otp_code(
    db: AsyncSession,
    redis: Redis,
    phone: str,
    code: str,
    *,
    consents: tuple[consent_svc.ConsentKind, ...] = (),
    ip: str | None = None,
    user_agent: str | None = None,
) -> tuple[TokenResponse, bool]:
    """
    Вход/регистрация по коду цели login. Возвращает (токены, is_new_user).

    consents — отметки, поставленные в форме входа. Новый аккаунт создаётся
    только со всеми обязательными (ConsentRequiredError). Проверка — ПОСЛЕ
    сжигания кода: иначе по ответу без кода можно было бы узнать, занят ли
    номер (enumeration). Фронт не даёт отправить форму без отметок, так что
    код сгорает только у нестандартного клиента.
    """
    await consume_otp_code(redis, code, purpose=OTPPurpose.login, subject=phone)

    # Find-or-create: подтверждённый номер = аутентифицированный
    # пользователь; отдельного шага «регистрация» нет.
    is_new_user = False
    user = await user_repo.get_user_by_phone(db, phone)
    if user is None and not set(consent_svc.ACCOUNT_KINDS) <= set(consents):
        raise consent_svc.ConsentRequiredError
    if user is None:
        try:
            user = await user_repo.create_user_by_phone(db, phone)
            is_new_user = True
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

    # Успешный ввод OTP доказывает владение номером — фиксируем явно.
    # Идемпотентно: повторный вход не плодит лишних UPDATE.
    if not user.is_phone_verified:
        user.is_phone_verified = True

    for kind in consents:
        await consent_svc.grant(db, user.id, kind, ip=ip, user_agent=user_agent)

    security_logger.info("otp_login_success user_id=%s", user.id)
    tokens = await issue_token_pair(db, user)
    tokens.is_new_user = is_new_user
    return tokens, is_new_user
