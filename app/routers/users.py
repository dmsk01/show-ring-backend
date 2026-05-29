import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.repositories.user import (
    get_profile,
    get_user_by_id,
    revoke_all_refresh_tokens_for_user,
    update_user,
    upsert_profile,
)
from app.schemas.user import (
    PublicUserResponse,
    UserProfileResponse,
    UserProfileUpdate,
    UserResponse,
    UserUpdate,
)
from app.utils.security import verify_password

# Отдельный логгер security-событий, чтобы можно было направлять в SIEM
# на этапе 14 (см. app/services/auth.py — тот же канал).
security_logger = logging.getLogger("app.security")

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
    description=(
        "Обновляет поля профиля. Смена email требует подтверждения "
        "текущим паролем (re-auth) и приводит к разлогину всех "
        "активных сессий пользователя."
    ),
)
async def change_user_info(
    update_data: UserUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # exclude_none, чтобы не затирать поля у БД None'ами от Pydantic.
    fields = update_data.model_dump(exclude_none=True)
    # current_password из payload используется ТОЛЬКО для re-auth и
    # никогда не попадает в БД. Изымаем заранее.
    current_password = fields.pop("current_password", None)

    email_changes = (
        "email" in fields and fields["email"] != current_user.email
    )
    if email_changes:
        # ИСПРАВЛЕНО (bug_203): три слоя защиты при смене email.
        # Раньше PUT /me менял email мгновенно по одному access-токену,
        # без re-auth и без отзыва сессий. Любой кто получил access-токен
        # (XSS, lost device, MITM) мог поменять email на свой и потом
        # через будущий "forgot password" забрать аккаунт.
        #
        # 1. Re-auth: пользователь должен предъявить текущий пароль.
        #    Случай "украли access-token, но пароль не знают" блокируется.
        if not current_password or not verify_password(
            current_password, current_user.hashed_password
        ):
            security_logger.warning(
                "email_change_bad_password user_id=%s", current_user.id
            )
            raise HTTPException(
                status_code=403, detail="current_password_invalid"
            )
        # 2. is_email_verified=False: новый email считается
        #    неподтверждённым до тех пор, пока пользователь не пройдёт
        #    отдельный verify-flow (TODO: см. tech-debt — отдельный
        #    эндпоинт verify-email-change с письмом на новый адрес).
        fields["is_email_verified"] = False
        # 3. Revoke all refresh tokens: если access-токен утёк и сейчас
        #    им пользуется атакующий, после этой операции у него
        #    останется только короткоживущий access (15 минут) — после
        #    его истечения refresh не сработает, законный владелец
        #    залогинится заново.
        await revoke_all_refresh_tokens_for_user(db, current_user.id)
        security_logger.info(
            "email_change user_id=%s old=%s new=%s",
            current_user.id, current_user.email, fields["email"],
        )

    # ИСПРАВЛЕНО: коллизия email (UNIQUE constraint) раньше валилась
    # в 500. Теперь отдаём корректный 409 Conflict.
    try:
        user = await update_user(db, current_user, **fields)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Email уже занят")
    return UserResponse.model_validate(user)


# УДАЛЕНО (bug_009 ultrareview): эндпоинт /users/admin/list
# назывался "Список пользователей (admin)", но возвращал ОДИН
# UserResponse — профиль самого вызывающего admin'а. Misleading
# название + redundant (полный список с пагинацией и ролями уже
# есть в /admin/users из routers/admin/moderation.py). Удалён,
# чтобы не плодить две точки правды и не путать клиентов API.


@router.get(
    "/me/profile",
    response_model=UserProfileResponse,
    summary="Мой профиль (ФИО/страна)",
)
async def get_my_profile(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    profile = await get_profile(db, current_user.id)
    if profile is None:
        # Профиль не заведён — отдаём пустой каркас, чтобы фронт показал
        # форму без 404.
        return UserProfileResponse()
    return UserProfileResponse.model_validate(profile)


@router.patch(
    "/me/profile",
    response_model=UserProfileResponse,
    summary="Обновить мой профиль (ФИО/страна)",
)
async def update_my_profile(
    payload: UserProfileUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    fields = payload.model_dump(exclude_unset=True)
    profile = await upsert_profile(db, current_user.id, **fields)
    await db.commit()
    return UserProfileResponse.model_validate(profile)


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
