from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession
from app.redis import get_redis
from redis.asyncio import Redis
from app.middleware.progressive_ban import check_rate_limit
from app.database import get_db
from app.services.auth import (
    refresh_access_token,
    register_user,
    verify_email,
    login_user,
    logout_user,
)
from app.schemas.user import RefreshRequest, TokenResponse, UserCreate

router = APIRouter(prefix="/auth", tags=["auth"])

# ИСПРАВЛЕНО: единое сообщение для register, чтобы не было user enumeration.
_REGISTER_RESPONSE = {"message": "Проверьте email для подтверждения"}


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
    "/login",
    summary="Вход в систему",
    description="Проверяет email и пароль, возвращает access token (15 мин) и refresh token (7 дней).",
)
async def login(
    request: Request,
    body: UserCreate,
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
        return await login_user(db, body.email, body.password)
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
        return await login_user(db, form.username, form.password)
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
    body: RefreshRequest,
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
        return await refresh_access_token(db, body.refresh_token)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))


@router.post(
    "/logout",
    summary="Выход из системы",
    description="Отзывает refresh token. После этого обновление access token становится невозможным.",
)
async def logout(
    request: Request,
    body: RefreshRequest,
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
    try:
        await logout_user(db, body.refresh_token)
        return {"message": "Успешный выход"}
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))
