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
from app.schemas.consent import ConsentGrantRequest, ConsentItem, ConsentsResponse
from app.schemas.dog import DogPage, DogResponse
from app.schemas.user import (
    AccountDeleteRequest,
    EmailLoginCreate,
    PasswordChange,
    PhoneSendCodeRequest,
    PhoneVerifyCodeRequest,
    PublicUserResponse,
    UserProfileResponse,
    UserProfileUpdate,
    UserResponse,
    UserSocialsResponse,
    UserSocialsUpdate,
    UserUpdate,
)
from app.services.account_security import (
    request_email_login,
    send_link_phone_code,
    send_reauth_code,
    verify_link_phone,
)
from app.services import consent as consent_svc
from app.services.account_deletion import delete_account
from app.services.auth import change_password, request_email_change
from app.services.otp_auth import OTPRateLimitedError
from app.services.sms import SMSDeliveryError, SMSProvider, get_sms_provider

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


# ---------------------------------------------------------------------
# Способы входа: привязка телефона, подключение входа по почте.
# OTP-ошибки кода — 400 (не 401), см. services/account_security.py.
# ---------------------------------------------------------------------

_CODE_SENT_RESPONSE = {"message": "Код отправлен"}


async def _send_otp_or_http(coro) -> dict:
    # Маппинг ошибок отправки — как у /auth/send-code.
    try:
        await coro
    except OTPRateLimitedError:
        raise HTTPException(status_code=429, detail="too_many_requests")
    except SMSDeliveryError:
        raise HTTPException(status_code=502, detail="sms_delivery_failed")
    return _CODE_SENT_RESPONSE


@router.post(
    "/me/phone/send-code",
    summary="Код для привязки телефона",
    description=(
        "Отправляет SMS-код на новый номер (E.164) для привязки к аккаунту. "
        "409 phone_already_set — у аккаунта уже есть подтверждённый номер; "
        "409 phone_taken — номер принадлежит другому аккаунту."
    ),
)
async def send_link_phone_code_endpoint(
    request: Request,
    body: PhoneSendCodeRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    sms: SMSProvider = Depends(get_sms_provider),
    current_user: User = Depends(get_current_user),
):
    await check_rate_limit(
        request, limit=5, window=60, redis=redis, fail_closed=True
    )
    return await _send_otp_or_http(
        send_link_phone_code(db, redis, sms, current_user, body.phone)
    )


@router.post(
    "/me/phone/verify",
    summary="Подтвердить привязку телефона",
    description=(
        "Проверяет код и записывает номер как подтверждённый. "
        "400 code_expired / invalid_code; 409 phone_taken / phone_already_set."
    ),
)
async def verify_link_phone_endpoint(
    request: Request,
    body: PhoneVerifyCodeRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current_user: User = Depends(get_current_user),
) -> UserResponse:
    await check_rate_limit(
        request, limit=10, window=60, redis=redis, fail_closed=True
    )
    ip = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")
    user = await verify_link_phone(
        db,
        redis,
        current_user,
        body.phone,
        body.code,
        ip=ip,
        user_agent=user_agent,
    )
    return UserResponse.model_validate(user)


@router.post(
    "/me/reauth/send-code",
    summary="Код подтверждения действия",
    description=(
        "Отправляет SMS-код на подтверждённый номер аккаунта. Нужен для "
        "действий, которые иначе требуют текущий пароль (подключение входа "
        "по почте). 409 phone_not_set — номера нет."
    ),
)
async def send_reauth_code_endpoint(
    request: Request,
    redis: Redis = Depends(get_redis),
    sms: SMSProvider = Depends(get_sms_provider),
    current_user: User = Depends(get_current_user),
):
    await check_rate_limit(
        request, limit=5, window=60, redis=redis, fail_closed=True
    )
    return await _send_otp_or_http(send_reauth_code(redis, sms, current_user))


@router.post(
    "/me/email-login",
    summary="Подключить вход по почте",
    description=(
        "Для аккаунта без email: задаёт пароль и отправляет ссылку "
        "подтверждения на адрес. Требует код из /users/me/reauth/send-code. "
        "Вход по почте работает после POST /auth/confirm-email-change. "
        "409 email_already_set / email_taken; 400 code_expired / invalid_code."
    ),
)
async def request_email_login_endpoint(
    request: Request,
    body: EmailLoginCreate,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current_user: User = Depends(get_current_user),
):
    await check_rate_limit(
        request, limit=5, window=3600, redis=redis, fail_closed=True
    )
    ip = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")
    await request_email_login(
        db,
        redis,
        current_user,
        body.email,
        body.password,
        body.code,
        ip=ip,
        user_agent=user_agent,
    )
    return {"message": "Проверьте почту для подтверждения"}


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
    "/me/socials",
    response_model=UserSocialsResponse,
    summary="Мои соцсети",
    description=(
        "Ссылки на соцсети текущего пользователя (Instagram, Facebook, "
        "VK, Telegram). Хранятся в том же профиле 1:1, что и ФИО/страна. "
        "Если профиль не заведён — отдаём пустой каркас, чтобы фронт "
        "показал форму без 404."
    ),
)
async def get_my_socials(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    profile = await get_profile(db, current_user.id)
    if profile is None:
        return UserSocialsResponse()
    return UserSocialsResponse.model_validate(profile)


@router.patch(
    "/me/socials",
    response_model=UserSocialsResponse,
    summary="Обновить мои соцсети",
    description=(
        "Частичное обновление: переданные поля сохраняются, остальные не "
        "трогаются. Пустая строка в поле очищает ссылку. Значения должны "
        "быть абсолютными http(s)-URL."
    ),
)
async def update_my_socials(
    payload: UserSocialsUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    fields = payload.model_dump(exclude_unset=True)
    profile = await upsert_profile(db, current_user.id, **fields)
    await db.commit()
    return UserSocialsResponse.model_validate(profile)


@router.post(
    "/me/delete",
    summary="Удалить аккаунт",
    description=(
        "Обезличивает аккаунт (ст. 21 152-ФЗ): удаляет телефон, email, "
        "профиль, объявления, обращения, подписки, сканы документов собак, "
        "контакты питомника; собаки и результаты выставок остаются без "
        "привязки к человеку. Подтверждение: code из "
        "/users/me/reauth/send-code (если подтверждён телефон) или password. "
        "409 active_shows / active_ad_campaigns — сначала завершите или "
        "передайте выставки и кампании."
    ),
)
async def delete_my_account(
    request: Request,
    body: AccountDeleteRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current_user: User = Depends(get_current_user),
):
    await check_rate_limit(
        request, limit=5, window=3600, redis=redis, fail_closed=True
    )
    await delete_account(
        db,
        redis,
        current_user,
        code=body.code,
        password=body.password,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    return {"message": "Аккаунт удалён"}


# ---------------------------------------------------------------------
# Согласия (152-ФЗ). Журнал — доказательство получения (ч. 3 ст. 9).
# ---------------------------------------------------------------------


async def _consents_response(db: AsyncSession, user: User) -> ConsentsResponse:
    active = await consent_svc.list_active(db, user.id)
    return ConsentsResponse(
        active=[ConsentItem.model_validate(c) for c in active],
        missing=consent_svc.missing_required(active),
    )


@router.get(
    "/me/consents",
    response_model=ConsentsResponse,
    summary="Мои согласия",
    description=(
        "Действующие согласия и список обязательных, которых нет в "
        "актуальной редакции документов (missing). Непустой missing — "
        "фронт просит подтвердить согласие (в т.ч. после новой редакции)."
    ),
)
async def get_my_consents(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await _consents_response(db, current_user)


@router.post(
    "/me/consents",
    response_model=ConsentsResponse,
    summary="Дать согласие",
    description="Принятие актуальной редакции Соглашения и/или согласия на обработку ПДн.",
)
async def grant_my_consents(
    request: Request,
    body: ConsentGrantRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ip = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")
    for kind in dict.fromkeys(body.kinds):
        await consent_svc.grant(
            db,
            current_user.id,
            consent_svc.ConsentKind(kind),
            ip=ip,
            user_agent=user_agent,
        )
    await db.commit()
    return await _consents_response(db, current_user)


@router.delete(
    "/me/consents/{kind}",
    response_model=ConsentsResponse,
    summary="Отозвать согласие",
    description=(
        "Отзыв согласия на обработку ПДн (ч. 2 ст. 9 152-ФЗ). Соглашение "
        "отдельно не отзывается — отказ от него = удаление аккаунта "
        "(400 use_account_deletion)."
    ),
)
async def revoke_my_consent(
    kind: consent_svc.ConsentKind,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if kind == consent_svc.ConsentKind.terms:
        raise HTTPException(status_code=400, detail="use_account_deletion")
    if kind != consent_svc.ConsentKind.personal_data:
        # Согласия на распространение отзываются переключателем у публикации.
        raise HTTPException(status_code=400, detail="use_publication_toggle")
    await consent_svc.revoke(db, current_user.id, kind)
    await db.commit()
    return await _consents_response(db, current_user)


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
