"""
Роутер уведомлений и подписок (этап 9).
"""

from __future__ import annotations

import logging
import uuid
from typing import NoReturn

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session_factory, get_db
from app.dependencies import (
    authenticate_ws,
    get_current_user,
    ws_rate_limit,
)
from app.models.notification import NotificationChannel
from app.models.user import User
from app.repositories import notification as repo
from app.schemas.notification import (
    MarkAllReadResponse,
    NotificationResponse,
    SubscriptionCreate,
    SubscriptionResponse,
    UnreadCountResponse,
)
from app.services.ws_manager import notif_ws_manager

logger = logging.getLogger(__name__)

router = APIRouter(tags=["notifications"])


def _raise_for_error(err: ValueError) -> NoReturn:
    code = str(err)
    if code == "not_found":
        raise HTTPException(404, code)
    if code == "forbidden":
        raise HTTPException(403, code)
    raise HTTPException(400, code)


# ---------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------


@router.post(
    "/subscriptions",
    response_model=SubscriptionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Подписаться на события",
)
async def create_subscription(
    body: SubscriptionCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # event_type приходит как enum — конвертируем в строку для БД,
    # потому что в модели поле String, а не SAEnum (см. комментарий
    # в models/notification.py — гибкость на новые типы без миграций).
    try:
        sub = await repo.create_subscription(
            db,
            user_id=user.id,
            event_type=body.event_type.value,
            filter_breed_id=body.filter_breed_id,
            filter_region=body.filter_region,
            channel=body.channel,
        )
    except IntegrityError:
        # UNIQUE-комбинация (user, event, breed, region, channel) —
        # пользователь уже подписан на это же.
        await db.rollback()
        raise HTTPException(409, "duplicate_subscription") from None
    return SubscriptionResponse.model_validate(sub)


@router.get(
    "/subscriptions",
    response_model=list[SubscriptionResponse],
    summary="Мои подписки",
)
async def list_subscriptions(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    items = await repo.list_user_subscriptions(db, user.id)
    return [SubscriptionResponse.model_validate(s) for s in items]


@router.delete(
    "/subscriptions/{subscription_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Отписаться",
)
async def delete_subscription(
    subscription_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    sub = await repo.get_subscription(db, subscription_id)
    if sub is None:
        raise HTTPException(404, "not_found")
    # Только владелец подписки может её удалить.
    if sub.user_id != user.id and not any(
        r.role.value == "admin" for r in user.roles
    ):
        raise HTTPException(403, "forbidden")
    await repo.delete_subscription(db, sub)


# ---------------------------------------------------------------------
# Notifications (лог)
# ---------------------------------------------------------------------


@router.get(
    "/notifications",
    response_model=list[NotificationResponse],
    summary="Мои уведомления",
)
async def list_my_notifications(
    # channel — необязательный фильтр по каналу (этап 16). Колокольчик
    # запрашивает ?channel=in_app, чтобы realtime-лента не смешивалась с
    # журналом email-рассылки. FastAPI сам валидирует значение enum.
    channel: NotificationChannel | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    items = await repo.list_user_notifications(
        db, user.id, channel=channel, page=page, per_page=per_page
    )
    return [NotificationResponse.model_validate(n) for n in items]


@router.get(
    "/notifications/unread-count",
    response_model=UnreadCountResponse,
    summary="Число непрочитанных уведомлений",
)
async def get_unread_count(
    # channel — по умолчанию in_app: бейдж колокольчика считает только
    # ленту, а не журнал email-доставки (иначе фантомный счётчик, см.
    # repo.count_unread_notifications). Можно переопределить ?channel=email.
    channel: NotificationChannel = Query(NotificationChannel.in_app),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    unread = await repo.count_unread_notifications(db, user.id, channel=channel)
    return UnreadCountResponse(unread=unread)


@router.patch(
    "/notifications/read-all",
    response_model=MarkAllReadResponse,
    summary="Отметить все уведомления прочитанными",
)
async def mark_all_read(
    # См. get_unread_count: по умолчанию закрываем in_app-ленту, не трогая
    # журнал email-доставки. Симметрично бейджу, чтобы «отметить всё» гасило
    # ровно то, что в нём посчитано.
    channel: NotificationChannel = Query(NotificationChannel.in_app),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    marked = await repo.mark_all_notifications_read(db, user.id, channel=channel)
    return MarkAllReadResponse(marked=marked)


@router.post(
    "/notifications/_dev/seed",
    response_model=list[NotificationResponse],
    summary="[dev] Накидать моковые уведомления (только при DEBUG)",
)
async def seed_mock_notifications(
    count: int = Query(5, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # Dev-only: в проде (settings.debug=False) ручка «не существует» —
    # отдаём 404, чтобы случайно не дать набивать БД моками. На dev
    # (DEBUG=true в docker-compose.dev.yml) — за один POST набивает
    # себе непрочитанных уведомлений для теста списка/бейджа/read-all.
    if not settings.debug:
        raise HTTPException(404, "not_found")
    items = await repo.create_mock_notifications(db, user.id, count)
    return [NotificationResponse.model_validate(n) for n in items]


@router.patch(
    "/notifications/{notification_id}/read",
    response_model=NotificationResponse,
    summary="Отметить уведомление прочитанным",
)
async def mark_notification_read(
    notification_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # Помечаем read_at только своему уведомлению. Идемпотентно: повторный
    # PATCH вернёт ту же запись с уже проставленным read_at. Чужое/
    # несуществующее → 404 (не раскрываем существование чужого).
    n = await repo.mark_notification_read(db, notification_id, user.id)
    if n is None:
        raise HTTPException(404, "not_found")
    return NotificationResponse.model_validate(n)


# ---------------------------------------------------------------------
# Realtime push (этап 16)
# ---------------------------------------------------------------------


@router.websocket("/ws/notifications")
async def notifications_ws(websocket: WebSocket):
    """
    Realtime-канал in-app уведомлений (колокольчик).

    Протокол:
    1. accept() без авторизации — handshake свободный.
    2. Клиент шлёт первый кадр {"type":"auth","token":"<access>"} с JWT
       (токен в сообщении, а не в URL — не светится в логах прокси).
    3. Сервер проверяет токен; при неудаче — close(4401).
    4. Сервер шлёт {"type":"auth_ok"} и регистрирует сокет в
       notif_ws_manager под ключом user.id.
    5. Дальше клиент НИЧЕГО не шлёт — только принимает пуши
       {"type":"notification","payload":<NotificationResponse>}, которые
       прилетают из events-воркера через Redis Pub/Sub (notif:{user_id}).
       Цикл receive_text нужен лишь чтобы поймать WebSocketDisconnect.

    Сессия БД открыта только на время handshake (как в чате поддержки,
    bug_205): отошедший клиент не держит соединение из пула.
    """
    await websocket.accept()

    # Rate-limit хендшейка по IP (10 connect/мин) — защита от флуда
    # соединений. При превышении ws_rate_limit сам закрывает сокет
    # (code=4429), нам остаётся выйти.
    if not await ws_rate_limit(websocket, limit=10, window=60):
        return

    # --- AUTH (короткая сессия БД только на handshake) ---
    try:
        first = await websocket.receive_json()
    except (WebSocketDisconnect, ValueError):
        await websocket.close(code=1003)  # unsupported_data
        return
    if not isinstance(first, dict) or first.get("type") != "auth":
        await websocket.send_json(
            {"type": "error", "payload": {"code": "auth_required"}}
        )
        await websocket.close(code=4401)
        return

    async with async_session_factory() as db:
        user = await authenticate_ws(db, first.get("token"), websocket)
    if user is None:
        await websocket.send_json(
            {"type": "error", "payload": {"code": "invalid_token"}}
        )
        await websocket.close(code=4401)
        return

    await websocket.send_json(
        {"type": "auth_ok", "payload": {"user_id": str(user.id)}}
    )
    # connect — после закрытия handshake-сессии: subscribe в Redis не
    # должен держать БД-коннект.
    await notif_ws_manager.connect(user.id, websocket)
    try:
        while True:
            # Клиент уведомлений не шлёт сообщений; receive держит
            # соединение и ловит разрыв. Пуши приходят НЕ отсюда, а из
            # notif_ws_manager._listen (Redis → сокет).
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("notifications WS loop error for user %s", user.id)
    finally:
        await notif_ws_manager.disconnect(user.id, websocket)
