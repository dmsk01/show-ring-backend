from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession
from jose import JWTError
from app.database import get_db
from app.models.user import User
from app.repositories.user import get_user_by_id
from app.utils.security import decode_access_token

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
