import asyncio
import logging
from uuid import UUID

from fastapi import Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from redis.asyncio import Redis
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession
from app import redis as redis_state
from app.config import settings
from app.database import get_db
from app.redis import get_redis
from app.middleware.progressive_ban import check_rate_limit
from app.models.user import User
from app.repositories.user import get_user_by_id
from app.utils.security import JWTError, decode_access_token

logger = logging.getLogger(__name__)

# Код закрытия WS при превышении rate-limit. 4xxx — приватный диапазон
# application-specific кодов закрытия (RFC 6455); 4429 выбран по аналогии
# с HTTP 429 Too Many Requests, чтобы клиент мог отличить флуд-отказ от
# обычного auth-разрыва (4401).
WS_CLOSE_RATE_LIMITED = 4429
# Ревью 2026-10-06, BE-21: чужой Origin (по аналогии с 403) и молчащий
# клиент, не приславший auth-кадр за отведённое время (по аналогии с 408).
WS_CLOSE_FORBIDDEN_ORIGIN = 4403
WS_CLOSE_AUTH_TIMEOUT = 4408
WS_AUTH_TIMEOUT_SECONDS = 10.0


def ws_origin_allowed(websocket: WebSocket) -> bool:
    """
    Origin WS-хендшейка: свой хост или разрешённый CORS-origin.

    CSRFMiddleware (HTTP-only) WebSocket не видит, а WS принимает
    httpOnly-куку из хендшейка — без этой проверки от cross-site
    WebSocket hijacking защищал только SameSite=Strict. Без Origin
    (мобильный клиент, curl) — пропускаем, как и для HTTP.
    """
    origin = websocket.headers.get("origin")
    if origin is None:
        return True
    secure = websocket.url.scheme in ("wss", "https")
    own = f"{'https' if secure else 'http'}://{websocket.url.netloc}"
    return origin == own or origin in settings.cors_allow_origins


async def ws_receive_auth_frame(websocket: WebSocket) -> dict | None:
    """
    Дождаться первого кадра (auth) не дольше WS_AUTH_TIMEOUT_SECONDS.

    None — сокет уже закрыт (таймаут → 4408, мусор/разрыв → 1003), и
    вызывающий обязан сделать return. Раньше ожидание было бесконечным:
    молчащий неаутентифицированный сокет держал слот --limit-concurrency.
    """
    try:
        first = await asyncio.wait_for(
            websocket.receive_json(), WS_AUTH_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        await websocket.close(code=WS_CLOSE_AUTH_TIMEOUT)
        return None
    except (WebSocketDisconnect, ValueError):
        await websocket.close(code=1003)  # unsupported_data
        return None
    return first if isinstance(first, dict) else {}


# ИСПРАВЛЕНО: tokenUrl указывает на form-эндпоинт /auth/token, который
# принимает OAuth2PasswordRequestForm. /auth/login по-прежнему живёт
# как JSON-эндпоинт для прикладных клиентов.
#
# auto_error=False: отсутствие заголовка Authorization — не ошибка, токен
# может прийти в httpOnly-куке access_token (веб). Заголовок приоритетнее
# куки — явная передача выигрывает у автоматической (мобилка, Swagger).
# 401 при полном отсутствии токена кидает сам get_current_user.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token", auto_error=False)


def _extract_access_token(request: Request, header_token: str | None) -> str | None:
    """Access-токен: заголовок Authorization → httpOnly-кука (веб)."""
    return header_token or request.cookies.get("access_token")


class _TokenRejected(Exception):
    """Токен не даёт доступа; detail — текст для 401."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


async def _user_from_token(db: AsyncSession, token: str) -> User:
    """
    Единая проверка access-токена (ревью 2026-10-06, BE-34): раньше одна и
    та же логика была скопирована в get_current_user, get_current_user_optional
    и authenticate_ws. Бросает _TokenRejected; вызывающий решает, это 401
    или просто «аноним».
    """
    try:
        payload = decode_access_token(token)
    except JWTError:
        raise _TokenRejected("Невалидный токен") from None
    # Явная проверка типа: refresh или иной JWT не годится как access.
    if payload.get("type") != "access":
        raise _TokenRejected("Невалидный токен")
    # UUID() на мусорном sub кидает ValueError — это 401, а не 500.
    try:
        uid = UUID(payload.get("sub") or "")
    except (ValueError, TypeError):
        raise _TokenRejected("Невалидный токен") from None
    user = await get_user_by_id(db, uid)
    if user is None:
        raise _TokenRejected("Невалидный токен")
    if not user.is_active:
        raise _TokenRejected("Пользователь заблокирован")
    return user


async def get_current_user_optional(
    request: Request,
    db: AsyncSession = Depends(get_db),
    header_token: str | None = Depends(oauth2_scheme),
) -> User | None:
    """Текущий пользователь или None (аудит H1): для публичных ручек, которым
    нужно ЗНАТЬ пользователя, если токен есть. Любая ошибка токена → None
    (не 401): публичная ручка продолжает работать как для анонима."""
    token = _extract_access_token(request, header_token)
    if not token:
        return None
    try:
        return await _user_from_token(db, token)
    except _TokenRejected:
        return None


def is_writer(user: User | None) -> bool:
    """admin или organizer — роль, которой можно писать/видеть черновики блога."""
    if user is None:
        return False
    return any(r.role.value in ("admin", "organizer") for r in user.roles)


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
    header_token: str | None = Depends(oauth2_scheme),
) -> User:
    token = _extract_access_token(request, header_token)
    if not token:
        # auto_error=False больше не кидает 401 сам — отвечаем как
        # OAuth2PasswordBearer, чтобы контракт для клиентов не поменялся.
        raise HTTPException(
            status_code=401,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return await _user_from_token(db, token)
    except _TokenRejected as e:
        raise HTTPException(status_code=401, detail=e.detail) from None


async def authenticate_ws(
    db: AsyncSession,
    token: str | None,
    websocket: WebSocket | None = None,
) -> User | None:
    """
    Аутентификация WebSocket-соединения по JWT, переданному ПЕРВЫМ
    сообщением (не в URL — токен не должен попадать в логи прокси),
    либо httpOnly-кукой access_token из хендшейка: веб-клиент токен
    не видит (JS нет доступа), но браузер сам прикладывает куки к
    upgrade-запросу. Мобильный клиент по-прежнему шлёт токен кадром.

    Возвращает активного User или None (роутер закрывает сокет с 4401).
    Раньше эта логика дублировалась приватной _authenticate_ws в
    routers/support.py; на этапе 16 вынесена сюда и переиспользуется
    обоими WS-роутами (поддержка + уведомления).
    """
    if not token and websocket is not None:
        token = websocket.cookies.get("access_token")
    if not token:
        return None
    try:
        return await _user_from_token(db, token)
    except _TokenRejected:
        return None


async def ws_rate_limit(
    websocket: WebSocket, *, limit: int, window: int
) -> bool:
    """
    Rate-limit WebSocket-хендшейка по IP (этап 16). Возвращает True, если
    соединение в пределах лимита; False — если превышен, и тогда сокет уже
    ЗАКРЫТ (code=4429) — вызывающий обязан сделать return.

    Переиспользует sliding-window логику check_rate_limit (тот же Lua,
    тот же sorted-set по IP+path), но транслирует HTTPException(429) в
    закрытие сокета — на принятом WS HTTP-статус уже не отдать.

    fail-open: Redis недоступен → пропускаем (как дешёвые публичные
    ручки). Connect — не аутентификация: открытое окно при сбое Redis
    тут менее критично, чем у login (там fail_closed).

    check_rate_limit типизирован под Request, но читает только
    .client/.scope/.url — у WebSocket они есть, поэтому передаём сокет
    как есть (см. type: ignore).
    """
    client = redis_state.redis_client
    if client is None:
        logger.debug("ws_rate_limit: Redis недоступен — fail-open")
        return True
    try:
        await check_rate_limit(websocket, limit, window, client)  # type: ignore[arg-type]
        return True
    except HTTPException:
        await websocket.close(code=WS_CLOSE_RATE_LIMITED)
        return False


def require_any_role(*roles: str):
    async def dependency(user: User = Depends(get_current_user)) -> User:
        user_roles = {r.role.value for r in user.roles}
        if not user_roles.intersection(roles):
            raise HTTPException(status_code=403, detail="Недостаточно прав")
        return user

    return dependency


# ИСПРАВЛЕНО (review 2026-05-28): один helper вместо копии в
# routers/classifieds.py, ads.py, tasks.py, shows.py. Не Dependency —
# это чистая функция: вызывается из сервисов / роутеров уже после
# get_current_user. Если завтра «кто такой admin» поменяется (например,
# появится super_admin), правка в одном месте.
def is_admin(user: User) -> bool:
    return any(r.role.value == "admin" for r in user.roles)


def user_rate_limit(bucket: str, *, limit: int, window: int):
    """
    Зависимость: лимит действия на ПОЛЬЗОВАТЕЛЯ (план защиты 2026-10-05).

    Ключ — user_id, а не IP: смена адреса лимит не обходит, а соседи по
    NAT друг другу не мешают. Тот же прогрессивный бан, что у
    check_rate_limit. fail-open: при сбое Redis действие не блокируем —
    это защита от злоупотреблений, а не от взлома.

        @router.post("/tickets", dependencies=[Depends(
            user_rate_limit("support:ticket", limit=5, window=3600))])
    """

    async def dependency(
        request: Request,
        user: User = Depends(get_current_user),
        redis: Redis = Depends(get_redis),
    ) -> None:
        await check_rate_limit(
            request,
            limit,
            window,
            redis,
            bucket=bucket,
            client_key=f"user:{user.id}",
        )

    return dependency

