from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.dependencies import get_current_user, require_any_role
from app.models.user import User
from app.repositories.user import get_user_by_id, update_user
from app.schemas.user import PublicUserResponse, UserResponse, UserUpdate

router = APIRouter(prefix="/users", tags=["users"])


@router.get(
    "/me",
    summary="Мой профиль",
    description="Возвращает профиль текущего авторизованного пользователя вместе с его ролями.",
)
async def get_user_info(current_user: User = Depends(get_current_user)):
    return UserResponse.model_validate(current_user)


@router.put(
    "/me",
    summary="Обновить профиль",
    description="Обновляет поля профиля. При смене email сбрасывает подтверждение почты.",
)
async def change_user_info(
    update_data: UserUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    fields = update_data.model_dump(exclude_none=True)
    if "email" in fields and fields["email"] != current_user.email:
        fields["is_email_verified"] = False
    # ИСПРАВЛЕНО: коллизия email (UNIQUE constraint) раньше валилась
    # в 500. Теперь отдаём корректный 409 Conflict.
    try:
        user = await update_user(db, current_user, **fields)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Email уже занят")
    return UserResponse.model_validate(user)


@router.get(
    "/admin/list",
    summary="Список пользователей (admin)",
    description="Возвращает профиль текущего пользователя. Доступен только администраторам.",
)
async def list_users_admin(
    current_user: User = Depends(require_any_role("admin")),
):
    return UserResponse.model_validate(current_user)


@router.get(
    "/{user_id}",
    summary="Публичный профиль",
    description="Возвращает публичный профиль пользователя по его UUID. Доступен без авторизации.",
    response_model=PublicUserResponse,
)
async def get_user(user_id: UUID, db: AsyncSession = Depends(get_db)):
    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    # ИСПРАВЛЕНО: PublicUserResponse без email/is_email_verified — раньше
    # неавторизованный мог собирать email'ы юзеров через перебор UUID.
    return PublicUserResponse.model_validate(user)
