import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.dependencies import get_current_user
from app.middleware.progressive_ban import check_rate_limit
from app.models.user import User
from app.redis import get_redis
from app.repositories import dog as dog_repo
from app.repositories.user import (
    get_profile,
    get_user_by_id,
    upsert_profile,
)
from app.schemas.dog import DogPage, DogResponse
from app.schemas.user import (
    PasswordChange,
    PublicUserResponse,
    UserProfileResponse,
    UserProfileUpdate,
    UserResponse,
    UserUpdate,
)
from app.services.auth import change_password, request_email_change

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
    summary="Запросить смену email",
    description=(
        "Запускает смену email через подтверждение. Требует текущий "
        "пароль (re-auth). Новый адрес НЕ применяется сразу — пишется "
        "в pending_email, а на него уходит письмо со ссылкой. Реальная "
        "смена и разлогин всех сессий — после POST /auth/confirm-email-change."
    ),
)
async def change_user_info(
    request: Request,
    update_data: UserUpdate,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current_user: User = Depends(get_current_user),
):
    # Этап 19: rate-limit на смену email — раньше эндпоинт был
    # единственным auth-чувствительным без защиты. fail_closed: при
    # сбое Redis закрываемся (см. progressive_ban / bug_247).
    await check_rate_limit(
        request, limit=5, window=3600, redis=redis, fail_closed=True
    )

    new_email = update_data.email
    if new_email is None or new_email == current_user.email:
        # Нечего менять — email тот же или не передан.
        return UserResponse.model_validate(current_user)

    ip = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")
    # Вся логика (re-auth, pending_email, токен, письмо, аудит, commit)
    # внутри сервиса — он же поднимает 403/409 с машиночитаемым detail.
    await request_email_change(
        db,
        current_user,
        new_email,
        update_data.current_password,
        ip=ip,
        user_agent=user_agent,
    )
    return {"message": "Проверьте новый email для подтверждения смены"}


@router.put(
    "/me/password",
    summary="Сменить пароль",
    description=(
        "Меняет пароль. Требует текущий пароль (re-auth). После смены "
        "все refresh-токены отзываются (разлогин на других устройствах), "
        "на текущий email уходит уведомление."
    ),
)
async def change_user_password(
    request: Request,
    payload: PasswordChange,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current_user: User = Depends(get_current_user),
):
    await check_rate_limit(
        request, limit=5, window=3600, redis=redis, fail_closed=True
    )
    ip = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")
    await change_password(
        db,
        current_user,
        payload.current_password,
        payload.new_password,
        ip=ip,
        user_agent=user_agent,
    )
    return {"message": "Пароль изменён"}


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
    "/me/dogs",
    response_model=DogPage,
    summary="Мои собаки",
    description=(
        "Собаки, владельцем которых является текущий пользователь "
        "(dog.owner_id == current_user.id). Включает собак без питомника. "
        "Отдельный путь, а не /dogs?mine=true: /dogs публичный (без auth), "
        "а здесь нужен пользователь. Сортировка по имени, пагинация как у /dogs."
    ),
)
async def list_my_dogs(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    items = await dog_repo.list_dogs(
        db,
        owner_id=current_user.id,
        sort_by="name",
        order="asc",
        page=page,
        per_page=per_page,
    )
    total = await dog_repo.count_dogs(db, owner_id=current_user.id)
    # Фото пачкой (анти-N+1), как в GET /dogs.
    photos = await dog_repo.photos_by_dogs(db, [d.id for d in items])
    return DogPage(
        items=[
            DogResponse.from_orm_with_photos(d, photos.get(d.id, []))
            for d in items
        ],
        total=total,
        page=page,
        per_page=per_page,
    )


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
