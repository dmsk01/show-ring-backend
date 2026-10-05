from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession
from app.redis import get_redis
from redis.asyncio import Redis
from app.middleware.progressive_ban import check_rate_limit
from app.config import settings
from app.database import get_db
from app.services.auth import (
    confirm_email_change,
    refresh_access_token,
    register_user,
    resend_verification,
    verify_email,
    login_user,
    logout_user,
)
from app.services.otp_auth import (
    OTPCountryNotAllowedError,
    OTPExpiredError,
    OTPInvalidError,
    OTPRateLimitedError,
    OTPUserBlockedError,
    SMSBudgetExceededError,
    send_otp_code,
    verify_otp_code,
)
from app.utils.net import ip_subnet
from app.services import login_guard
from app.services.captcha import require_captcha
from app.services.email_tasks import enqueue_transactional_email
from app.repositories import user as user_repo
from app.services.consent import ConsentKind, ConsentRequiredError
from app.services.consent import grant as consent_grant
from app.services.sms import SMSDeliveryError, SMSProvider, get_sms_provider
from app.services.auth_methods import enabled_auth_methods, primary_auth_method
from app.schemas.user import (
    AuthMethodItem,
    AuthMethodsResponse,
    EmailChangeConfirm,
    PhoneSendCodeRequest,
    PhoneVerifyCodeRequest,
    RefreshRequest,
    ResendVerification,
    TokenResponse,
    UserCreate,
    UserLogin,
)

# Анти-enumeration: ответ одинаков, существует адрес или нет.
_RESEND_RESPONSE = {"message": "Если адрес не подтверждён, письмо отправлено"}

router = APIRouter(prefix="/auth", tags=["auth"])

# ИСПРАВЛЕНО: единое сообщение для register, чтобы не было user enumeration.
_REGISTER_RESPONSE = {"message": "Проверьте email для подтверждения"}

# Анти-enumeration: ответ одинаков для нового и существующего номера.
_SEND_CODE_RESPONSE = {"message": "Код отправлен"}


# Заголовок, которым клиент просит токены в теле ответа (React Native:
# у мобильного приложения нет кук). Без заголовка — режим по умолчанию:
# оба токена в httpOnly-куках, в теле null (XSS-устойчиво для веба).
_TOKEN_DELIVERY_HEADER = "X-Token-Delivery"

# Общий ключ rate-limit'а для /auth/login и /auth/token (это один логин).
_LOGIN_RATE_BUCKET = "/auth/login"


def _access_cookie_path() -> str:
    # path должен совпадать с ПУБЛИЧНЫМ путём API (за nginx — /api/...),
    # браузер матчит куку по URL, который видит сам. См. cookie_path_prefix.
    return settings.cookie_path_prefix.rstrip("/") or "/"


def _refresh_cookie_path() -> str:
    # Refresh-кука уходит только на /auth/* (refresh, logout) —
    # минимизирует поверхность утечки.
    return settings.cookie_path_prefix.rstrip("/") + "/auth"


def _set_token_cookie(
    response: Response, name: str, value: str, max_age: int, path: str
) -> None:
    response.set_cookie(
        name,
        value,
        httponly=True,
        secure=not settings.debug,
        samesite="strict",
        max_age=max_age,
        path=path,
    )


def _deliver_tokens(
    request: Request, response: Response, tokens: TokenResponse
) -> TokenResponse:
    """
    Доставка пары токенов. По умолчанию (веб) — оба в httpOnly-куках,
    в теле access_token/refresh_token = null. С заголовком
    `X-Token-Delivery: body` (мобильный клиент) — в теле, без кук.
    """
    if request.headers.get(_TOKEN_DELIVERY_HEADER, "").lower() == "body":
        return tokens
    if tokens.access_token:
        _set_token_cookie(
            response,
            "access_token",
            tokens.access_token,
            max_age=settings.access_token_expire_minutes * 60,
            path=_access_cookie_path(),
        )
        tokens.access_token = None
    if tokens.refresh_token:
        _set_token_cookie(
            response,
            "refresh_token",
            tokens.refresh_token,
            max_age=settings.refresh_token_expire_days * 86400,
            path=_refresh_cookie_path(),
        )
        tokens.refresh_token = None
    return tokens


def _ensure_email_login_enabled() -> None:
    # Email — дополнительный способ входа, может быть выключен флагом.
    if not settings.auth_email_login_enabled:
        raise HTTPException(status_code=403, detail="login_method_disabled")


def _extract_refresh(request: Request, body: RefreshRequest) -> str:
    # Тело (мобильный клиент) → кука (веб). Кука читается всегда:
    # cookie-режим — дефолт, глобального флага больше нет.
    raw = body.refresh_token or request.cookies.get("refresh_token")
    if not raw:
        raise HTTPException(status_code=401, detail="missing_refresh_token")
    return raw


@router.get(
    "/methods",
    summary="Доступные способы входа и регистрации",
    description=(
        "Реестр способов аутентификации: клиент рисует экраны входа и "
        "регистрации по нему. primary — способ по умолчанию; "
        "phone_required — каждый аккаунт должен иметь подтверждённый телефон."
    ),
)
async def auth_methods() -> AuthMethodsResponse:
    return AuthMethodsResponse(
        primary=primary_auth_method(),
        phone_required=settings.auth_phone_required,
        methods=[
            AuthMethodItem(id=m.id, sign_in=m.sign_in, sign_up=m.sign_up)
            for m in enabled_auth_methods()
        ],
    )


@router.post(
    "/register",
    summary="Регистрация по email (по умолчанию выключена)",
    description=(
        "Создаёт аккаунт и отправляет письмо с ссылкой для подтверждения "
        "email. Основной способ регистрации — по телефону "
        "(/auth/send-code + /auth/verify-code); регистрация по email "
        "включается флагом AUTH_EMAIL_REGISTRATION_ENABLED, иначе 403."
    ),
)
async def register(
    request: Request,
    body: UserCreate,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    # bug_247 audit 2026-05-28: fail_closed=True на всех auth-эндпоинтах.
    # Без этого падение Redis превращалось в открытое окно для
    # credential stuffing'а / spam-регистраций / token-guessing'а —
    # rate-limit беззвучно отключался, и атакующий получал unlimited
    # попытки. Теперь Redis-сбой → 503, что отказ обслуживания, но
    # лучше, чем компрометация аккаунтов. Тот же fail_closed=True
    # стоит и на остальных auth-callsite'ах ниже — повторяю без
    # комментария, чтобы не зашумлять файл.
    await check_rate_limit(
        request,
        limit=settings.auth_register_rate_limit,
        window=settings.auth_register_rate_window_seconds,
        redis=redis,
        fail_closed=True,
    )
    # Регистрация — только по телефону. Флаг проверяется ПОСЛЕ rate-limit:
    # закрытый эндпоинт не должен становиться бесплатным для долбёжки.
    if not settings.auth_email_registration_enabled:
        raise HTTPException(
            status_code=403, detail="registration_method_disabled"
        )
    # ИСПРАВЛЕНО: ответ одинаков и для нового, и для уже существующего
    # email — это защита от перечисления учётных записей. Сервис
    # возвращает None в случае коллизии, мы это не светим наружу.
    user = await register_user(db, body.email, body.password)
    if user is not None:
        for kind, given in (
            (ConsentKind.terms, body.accept_terms),
            (ConsentKind.personal_data, body.personal_data_consent),
        ):
            if given:
                await consent_grant(
                    db,
                    user.id,
                    kind,
                    ip=request.client.host if request.client else None,
                    user_agent=request.headers.get("user-agent"),
                )
        await db.commit()
    return _REGISTER_RESPONSE


@router.post(
    "/verify-email",
    summary="Подтверждение email",
    description="Принимает одноразовый токен из письма и активирует email пользователя.",
)
async def verify_user_email(
    request: Request,
    token: str = Query(...),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    await check_rate_limit(
        request,
        limit=10,
        window=60,
        redis=redis,
        fail_closed=True,  # bug_247: см. /register
    )
    try:
        await verify_email(db, token)
        return {"message": "Email подтверждён"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post(
    "/resend-verification",
    summary="Повторная отправка письма подтверждения",
    description=(
        "Повторно отправляет письмо подтверждения email. Ответ одинаков "
        "независимо от существования адреса (защита от перечисления). "
        "Жёсткий rate-limit: 3 запроса в час."
    ),
)
async def resend_verification_endpoint(
    request: Request,
    body: ResendVerification,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    await check_rate_limit(
        request, limit=3, window=3600, redis=redis, fail_closed=True
    )
    await resend_verification(db, body.email)
    return _RESEND_RESPONSE


@router.post(
    "/confirm-email-change",
    summary="Подтверждение смены email",
    description=(
        "Принимает токен из письма, переносит pending_email в email, "
        "помечает email подтверждённым и отзывает все refresh-токены."
    ),
)
async def confirm_email_change_endpoint(
    request: Request,
    body: EmailChangeConfirm,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    await check_rate_limit(
        request, limit=10, window=60, redis=redis, fail_closed=True
    )
    ip = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")
    await confirm_email_change(
        db, body.token, ip=ip, user_agent=user_agent
    )
    return {"message": "Email изменён"}


async def _guarded_login(
    request: Request,
    response: Response,
    db: AsyncSession,
    redis: Redis,
    email: str,
    password: str,
    captcha: str | None,
) -> TokenResponse:
    """Вход по паролю под защитой login_guard: капча и блокировка аккаунта."""
    ip = request.client.host if request.client else "unknown"
    await login_guard.check_before_login(redis, email=email, ip=ip, captcha=captcha)
    try:
        tokens = await login_user(db, email, password)
    except ValueError as e:
        if str(e) == "invalid_credentials":
            locked_now = await login_guard.record_failure(redis, email=email, ip=ip)
            if locked_now:
                await _notify_account_locked(db, email, ip)
        raise HTTPException(status_code=401, detail=str(e))
    await login_guard.record_success(redis, email=email)
    return _deliver_tokens(request, response, tokens)


async def _notify_account_locked(db: AsyncSession, email: str, ip: str) -> None:
    """Письмо владельцу о блокировке (только если адрес принадлежит аккаунту)."""
    user = await user_repo.get_user_by_email(db, email)
    if user is None or not user.email:
        return
    await enqueue_transactional_email(
        db,
        user_id=user.id,
        to_email=user.email,
        template_name="account_locked",
        context={"minutes": settings.login_lockout_seconds // 60, "ip": ip},
    )
    await db.commit()


@router.post(
    "/login",
    summary="Вход в систему",
    description=(
        "Проверяет email и пароль. По умолчанию ставит access (15 мин) и "
        "refresh (7 дней) токены в httpOnly-куки, в теле — null. С "
        "заголовком X-Token-Delivery: body (мобильный клиент) — токены "
        "в теле ответа, без кук."
    ),
)
async def login(
    request: Request,
    body: UserLogin,
    response: Response,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> TokenResponse:
    await check_rate_limit(
        request,
        limit=settings.auth_login_rate_limit,
        window=settings.auth_login_rate_window_seconds,
        redis=redis,
        fail_closed=True,  # bug_247: см. /register
        # Общий счётчик с /auth/token: раньше ключ включал путь, и
        # чередование двух ручек удваивало число попыток подбора пароля.
        bucket=_LOGIN_RATE_BUCKET,
    )
    _ensure_email_login_enabled()
    return await _guarded_login(
        request, response, db, redis, body.email, body.password, body.captcha
    )


@router.post(
    "/token",
    summary="OAuth2-совместимый login (form-data)",
    description=(
        "Альтернативный логин на form-data (username/password) — нужен для "
        "кнопки 'Authorize' в Swagger. Возвращает тот же TokenResponse, что "
        "и /auth/login. Используй /auth/login для обычной JSON-интеграции."
    ),
)
async def login_form(
    request: Request,
    response: Response,
    form: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> TokenResponse:
    # ИСПРАВЛЕНО: добавлен form-эндпоинт, чтобы tokenUrl в OAuth2PasswordBearer
    # совпадал с реальной реализацией. Раньше Swagger Authorize не работал.
    # bug_247: см. /register — fail_closed для всех auth-callsite'ов.
    # /token — тот же логин (form-data для Swagger), делит лимит с /auth/login.
    await check_rate_limit(
        request,
        limit=settings.auth_login_rate_limit,
        window=settings.auth_login_rate_window_seconds,
        redis=redis,
        fail_closed=True,
        # Общий счётчик с /auth/token: раньше ключ включал путь, и
        # чередование двух ручек удваивало число попыток подбора пароля.
        bucket=_LOGIN_RATE_BUCKET,
    )
    _ensure_email_login_enabled()
    # OAuth2 спецификация требует поле username — мапим его на email.
    # Поля для капчи в форме нет: когда она станет нужна — 400
    # captcha_required, вход через /auth/login.
    return await _guarded_login(
        request, response, db, redis, form.username, form.password, None
    )


@router.post(
    "/refresh",
    summary="Обновление access token",
    description=(
        "Принимает refresh token, возвращает новый access + новый refresh. "
        "Старый refresh после успешного вызова становится недействительным "
        "(rotation): повторный запрос с тем же токеном даёт 401."
    ),
)
async def refresh(
    request: Request,
    response: Response,
    body: RefreshRequest = RefreshRequest(),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> TokenResponse:
    await check_rate_limit(
        request,
        limit=5,
        window=60,
        redis=redis,
        fail_closed=True,  # bug_247: см. /register
    )
    try:
        # ИСПРАВЛЕНО: возвращаем TokenResponse целиком — клиент обязан
        # заменить refresh-токен. См. rotation в services.auth.refresh_access_token.
        return _deliver_tokens(
            request, response, await refresh_access_token(db, _extract_refresh(request, body))
        )
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))


@router.post(
    "/logout",
    summary="Выход из системы",
    description="Отзывает refresh token. После этого обновление access token становится невозможным.",
)
async def logout(
    request: Request,
    response: Response,
    body: RefreshRequest = RefreshRequest(),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    await check_rate_limit(
        request,
        limit=5,
        window=60,
        redis=redis,
        fail_closed=True,  # bug_247: см. /register
    )
    # Куки чистим всегда, даже если токен уже отозван — иначе браузер
    # остаётся с невалидными куками и получает 401 на каждом запросе.
    response.delete_cookie("access_token", path=_access_cookie_path())
    response.delete_cookie("refresh_token", path=_refresh_cookie_path())
    try:
        await logout_user(db, _extract_refresh(request, body))
        return {"message": "Успешный выход"}
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))


@router.post(
    "/send-code",
    summary="Отправка OTP-кода на телефон",
    description=(
        "Принимает номер в E.164, отправляет SMS с одноразовым кодом "
        "(TTL 5 минут). Повторная отправка на тот же номер — не чаще "
        "раза в 60 секунд (429). Ответ одинаков для нового и "
        "существующего номера (анти-enumeration)."
    ),
)
async def send_code(
    request: Request,
    body: PhoneSendCodeRequest,
    redis: Redis = Depends(get_redis),
    sms: SMSProvider = Depends(get_sms_provider),
):
    # IP-лимит поверх per-phone cooldown'а: cooldown не мешает перебирать
    # РАЗНЫЕ номера с одного IP (SMS pumping). bug_247: fail_closed.
    await check_rate_limit(
        request, limit=5, window=60, redis=redis, fail_closed=True
    )
    # Лимит на подсеть: пул прокси обходит лимит «на IP», но обычно
    # сидит в соседних адресах (анти SMS pumping, план 2026-10-05).
    client_ip = request.client.host if request.client else "unknown"
    await check_rate_limit(
        request,
        limit=settings.otp_subnet_limit,
        window=3600,
        redis=redis,
        fail_closed=True,
        bucket="send-code:subnet",
        client_key=ip_subnet(client_ip),
    )
    # Капча — после дешёвых лимитов, но до любой работы с SMS: каждое SMS
    # стоит денег, а решение задачи стоит боту CPU.
    await require_captcha(redis, body.captcha)
    try:
        await send_otp_code(redis, sms, body.phone)
    except OTPCountryNotAllowedError:
        raise HTTPException(status_code=400, detail="country_not_supported")
    except SMSBudgetExceededError:
        raise HTTPException(status_code=503, detail="sms_unavailable")
    except OTPRateLimitedError:
        raise HTTPException(status_code=429, detail="too_many_requests")
    except SMSDeliveryError:
        # Детали провайдера наружу не отдаём; cooldown уже стоит.
        raise HTTPException(status_code=502, detail="sms_delivery_failed")
    return _SEND_CODE_RESPONSE


@router.post(
    "/verify-code",
    summary="Вход/регистрация по OTP-коду",
    description=(
        "Проверяет код из SMS (максимум 3 попытки, затем код сжигается). "
        "При первом входе создаёт пользователя по номеру. Возвращает "
        "access + refresh (по умолчанию — в httpOnly-куках; с заголовком "
        "X-Token-Delivery: body — в теле). is_new_user=true — аккаунт "
        "создан этим входом. 400 — неверный код, 401 — код истёк/исчерпан."
    ),
)
async def verify_code(
    request: Request,
    body: PhoneVerifyCodeRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> TokenResponse:
    await check_rate_limit(
        request, limit=10, window=60, redis=redis, fail_closed=True
    )
    consents = tuple(
        kind
        for kind, given in (
            (ConsentKind.terms, body.accept_terms),
            (ConsentKind.personal_data, body.personal_data_consent),
        )
        if given
    )
    try:
        tokens, _ = await verify_otp_code(
            db,
            redis,
            body.phone,
            body.code,
            consents=consents,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    except ConsentRequiredError:
        raise HTTPException(status_code=400, detail="consent_required")
    except OTPExpiredError:
        raise HTTPException(status_code=401, detail="code_expired")
    except OTPUserBlockedError:
        raise HTTPException(status_code=401, detail="user_blocked")
    except OTPInvalidError:
        raise HTTPException(status_code=400, detail="invalid_code")
    return _deliver_tokens(request, response, tokens)
