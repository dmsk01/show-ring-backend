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
    OTPExpiredError,
    OTPInvalidError,
    OTPRateLimitedError,
    OTPUserBlockedError,
    send_otp_code,
    verify_otp_code,
)
from app.services.sms import SMSDeliveryError, SMSProvider, get_sms_provider
from app.schemas.user import (
    EmailChangeConfirm,
    PhoneSendCodeRequest,
    PhoneVerifyCodeRequest,
    RefreshRequest,
    ResendVerification,
    TokenResponse,
    UserCreate,
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


def _extract_refresh(request: Request, body: RefreshRequest) -> str:
    # Тело (мобильный клиент) → кука (веб). Кука читается всегда:
    # cookie-режим — дефолт, глобального флага больше нет.
    raw = body.refresh_token or request.cookies.get("refresh_token")
    if not raw:
        raise HTTPException(status_code=401, detail="missing_refresh_token")
    return raw


@router.post(
    "/register",
    summary="Регистрация пользователя",
    description="Создаёт аккаунт и отправляет письмо с ссылкой для подтверждения email.",
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
        limit=3,
        window=3600,
        redis=redis,
        fail_closed=True,
    )
    # ИСПРАВЛЕНО: ответ одинаков и для нового, и для уже существующего
    # email — это защита от перечисления учётных записей. Сервис
    # возвращает None в случае коллизии, мы это не светим наружу.
    await register_user(db, body.email, body.password)
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
    body: UserCreate,
    response: Response,
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
        return _deliver_tokens(
            request, response, await login_user(db, body.email, body.password)
        )
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))


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
    await check_rate_limit(
        request, limit=5, window=60, redis=redis, fail_closed=True
    )
    try:
        # OAuth2 спецификация требует поле username — мапим его на email.
        return _deliver_tokens(
            request, response, await login_user(db, form.username, form.password)
        )
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))


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
    try:
        await send_otp_code(redis, sms, body.phone)
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
        "X-Token-Delivery: body — в теле). 400 — неверный код, 401 — код "
        "истёк/исчерпан."
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
    try:
        tokens = await verify_otp_code(db, redis, body.phone, body.code)
    except OTPExpiredError:
        raise HTTPException(status_code=401, detail="code_expired")
    except OTPUserBlockedError:
        raise HTTPException(status_code=401, detail="user_blocked")
    except OTPInvalidError:
        raise HTTPException(status_code=400, detail="invalid_code")
    return _deliver_tokens(request, response, tokens)
