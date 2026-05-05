from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession
from jose import JWTError
from app.database import get_db
from app.models.user import User
from app.repositories.user import get_user_by_id
from app.utils.security import decode_access_token

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


async def get_current_user(
    db: AsyncSession = Depends(get_db), token: str = Depends(oauth2_scheme)
):
    try:
        payload = decode_access_token(token)
        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=401, detail="Невалидный токен")
        user = await get_user_by_id(db, UUID(user_id))
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
