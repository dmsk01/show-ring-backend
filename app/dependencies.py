import logging
from uuid import UUID

from fastapi import Depends, HTTPException, WebSocket, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession
from jose import JWTError
from app import redis as redis_state
from app.database import get_db
from app.middleware.progressive_ban import check_rate_limit
from app.models.user import User
from app.repositories.user import get_user_by_id
from app.utils.security import decode_access_token

logger = logging.getLogger(__name__)

# Код закрытия WS при превышении rate-limit. 4xxx — приватный диапазон
# application-specific кодов закрытия (RFC 6455); 4429 выбран по аналогии
# с HTTP 429 Too Many Requests, чтобы клиент мог отличить флуд-отказ от
# обычного auth-разрыва (4401).
WS_CLOSE_RATE_LIMITED = 4429

# ИСПРАВЛЕНО: tokenUrl указывает на form-эндпоинт /auth/token, который
# принимает OAuth2PasswordRequestForm. /auth/login по-прежнему живёт
# как JSON-эндпоинт для прикладных клиентов.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token")


async def get_current_user(
    db: AsyncSession = Depends(get_db), token: str = Depends(oauth2_scheme)
):
    try:
        payload = decode_access_token(token)
        # ИСПРАВЛЕНО: явная проверка типа токена — защита от случая,
        # когда в /auth/login начнут возвращать JWT и для refresh.
        if payload.get("type") != "access":
            raise HTTPException(status_code=401, detail="Невалидный токен")
        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=401, detail="Невалидный токен")
        # ИСПРАВЛЕНО: UUID() кидает ValueError при некорректном sub —
        # раньше пробрасывалось в ErrorHandler → 500. Теперь 401.
        try:
            uid = UUID(user_id)
        except (ValueError, TypeError):
            raise HTTPException(status_code=401, detail="Невалидный токен")
        user = await get_user_by_id(db, uid)
        if not user:
            raise HTTPException(status_code=401, detail="Невалидный токен")
        if not user.is_active:
            raise HTTPException(status_code=401, detail="Пользователь заблокирован")
        return user
    except JWTError:
        raise HTTPException(status_code=401, detail="Невалидный токен")


async def authenticate_ws(db: AsyncSession, token: str | None) -> User | None:
    """
    Аутентификация WebSocket-соединения по JWT, переданному ПЕРВЫМ
    сообщением (не в URL — токен не должен попадать в логи прокси).

    Возвращает активного User или None (роутер закрывает сокет с 4401).
    Раньше эта логика дублировалась приватной _authenticate_ws в
    routers/support.py; на этапе 16 вынесена сюда и переиспользуется
    обоими WS-роутами (поддержка + уведомления).
    """
    if not token:
        return None
    try:
        payload = decode_access_token(token)
    except JWTError:
        return None
    if payload.get("type") != "access":
        return None
    try:
        uid = UUID(payload.get("sub", ""))
    except (ValueError, TypeError):
        return None
    user = await get_user_by_id(db, uid)
    if user is None or not user.is_active:
        return None
    return user


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
