"""
Роутер поддержки (этап 11).

REST + WebSocket. WebSocket-эндпоинт принимает JWT через ПЕРВОЕ сообщение
(не в query/path), чтобы токен не попадал в URL-логи прокси и историю
браузера.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import NoReturn
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from jose import JWTError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_factory, get_db
from app.dependencies import get_current_user, require_any_role
from app.models.support import TicketStatus
from app.models.user import User
from app.repositories import support as repo
from app.repositories.user import get_user_by_id
from app.schemas.support import (
    MessageCreate,
    MessageResponse,
    TicketAssignRequest,
    TicketCreate,
    TicketResponse,
    TicketStatusUpdate,
)
from app.services import support as svc
from app.services.ws_manager import ws_manager
from app.utils.security import decode_access_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/support", tags=["support"])


def _raise_for_error(err: ValueError) -> NoReturn:
    code = str(err)
    if code == "not_found":
        raise HTTPException(404, code)
    if code == "forbidden":
        raise HTTPException(403, code)
    raise HTTPException(400, code)


# ---------------------------------------------------------------------
# Tickets — REST
# ---------------------------------------------------------------------


@router.post(
    "/tickets",
    response_model=TicketResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Создать тикет в поддержку",
)
async def create_ticket(
    body: TicketCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ticket, _msg = await svc.create_ticket(
        db,
        user_id=user.id,
        subject=body.subject,
        body=body.body,
        priority=body.priority,
    )
    return TicketResponse.model_validate(ticket)


@router.get(
    "/tickets",
    response_model=list[TicketResponse],
    summary="Мои тикеты",
)
async def list_my_tickets(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    items = await repo.list_user_tickets(db, user.id)
    return [TicketResponse.model_validate(t) for t in items]


@router.get(
    "/tickets/{ticket_id}",
    response_model=TicketResponse,
    summary="Карточка тикета",
)
async def get_ticket(
    ticket_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    obj = await repo.get_ticket(db, ticket_id)
    if obj is None:
        raise HTTPException(404, "not_found")
    if not svc.can_access_ticket(obj, user):
        raise HTTPException(403, "forbidden")
    return TicketResponse.model_validate(obj)


@router.put(
    "/tickets/{ticket_id}/status",
    response_model=TicketResponse,
    summary="Сменить статус тикета (operator/admin)",
)
async def change_ticket_status(
    ticket_id: uuid.UUID,
    body: TicketStatusUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        obj = await svc.change_status(db, ticket_id, user, body.status)
    except ValueError as e:
        _raise_for_error(e)
    return TicketResponse.model_validate(obj)


@router.put(
    "/tickets/{ticket_id}/assign",
    response_model=TicketResponse,
    summary="Назначить оператора (admin)",
)
async def assign_ticket(
    ticket_id: uuid.UUID,
    body: TicketAssignRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        obj = await svc.assign_operator(
            db, ticket_id, user, body.assigned_to_id
        )
    except ValueError as e:
        _raise_for_error(e)
    return TicketResponse.model_validate(obj)


# ---------------------------------------------------------------------
# Messages — REST
# ---------------------------------------------------------------------


@router.get(
    "/tickets/{ticket_id}/messages",
    response_model=list[MessageResponse],
    summary="История сообщений",
)
async def list_messages(
    ticket_id: uuid.UUID,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ticket = await repo.get_ticket(db, ticket_id)
    if ticket is None:
        raise HTTPException(404, "not_found")
    if not svc.can_access_ticket(ticket, user):
        raise HTTPException(403, "forbidden")
    items = await repo.list_messages(
        db, ticket_id, page=page, per_page=per_page
    )
    # Помечаем прочитанными «не свои» сообщения — REST как авто-action
    # на просмотр истории.
    await repo.mark_messages_read(db, ticket_id, svc.is_operator(user))
    return [MessageResponse.model_validate(m) for m in items]


@router.post(
    "/tickets/{ticket_id}/messages",
    response_model=MessageResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Отправить сообщение (REST fallback)",
)
async def post_message_rest(
    ticket_id: uuid.UUID,
    body: MessageCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        msg = await svc.post_message(db, ticket_id, user, body.body)
    except ValueError as e:
        _raise_for_error(e)
    # Распространяем в WS-клиентов, чтобы открытые чаты подхватили.
    await ws_manager.publish(
        ticket_id,
        {
            "type": "message",
            "payload": _serialize_message(msg),
        },
    )
    return MessageResponse.model_validate(msg)


# ---------------------------------------------------------------------
# WebSocket-чат
# ---------------------------------------------------------------------


def _serialize_message(msg) -> dict:
    """JSON-сериализация сообщения для WS-кадра (datetime → ISO)."""
    return {
        "id": str(msg.id),
        "ticket_id": str(msg.ticket_id),
        "sender_id": str(msg.sender_id) if msg.sender_id else None,
        "is_from_operator": msg.is_from_operator,
        "body": msg.body,
        "is_read": msg.is_read,
        "created_at": (
            msg.created_at.isoformat()
            if isinstance(msg.created_at, datetime)
            else str(msg.created_at)
        ),
    }


async def _authenticate_ws(
    db: AsyncSession, token: str
) -> User | None:
    """Декодирует JWT (тот же формат, что и в REST) и возвращает User."""
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


@router.websocket("/ws/{ticket_id}")
async def support_ws(websocket: WebSocket, ticket_id: uuid.UUID):
    """
    Real-time чат поддержки.

    Протокол:
    1. accept() без авторизации — handshake свободный.
    2. Клиент шлёт первый кадр {"type":"auth","token":"..."} с JWT.
    3. Сервер проверяет токен + доступ к тикету; если нет — error+close.
    4. Сервер шлёт {"type":"auth_ok"}.
    5. Дальше клиент шлёт {"type":"message","body":"..."} — сервер
       сохраняет в БД и публикует в Redis-канал тикета. Все подключенные
       сокеты (на всех инстансах) получают копию.
    """
    await websocket.accept()

    # Каждый WS-цикл живёт долго, своя сессия БД (не Depends, потому
    # что dependency для WS — отдельный механизм).
    async with async_session_factory() as db:
        # --- AUTH ---
        try:
            first = await websocket.receive_json()
        except (WebSocketDisconnect, ValueError):
            await websocket.close(code=1003)  # unsupported_data
            return
        if first.get("type") != "auth" or "token" not in first:
            await websocket.send_json(
                {
                    "type": "error",
                    "payload": {"code": "auth_required", "detail": "First frame must be {type:auth,token:...}"},
                }
            )
            await websocket.close(code=4401)
            return
        user = await _authenticate_ws(db, first["token"])
        if user is None:
            await websocket.send_json(
                {"type": "error", "payload": {"code": "invalid_token"}}
            )
            await websocket.close(code=4401)
            return

        # --- ACCESS CHECK ---
        ticket = await repo.get_ticket(db, ticket_id)
        if ticket is None:
            await websocket.send_json(
                {"type": "error", "payload": {"code": "ticket_not_found"}}
            )
            await websocket.close(code=4404)
            return
        if not svc.can_access_ticket(ticket, user):
            await websocket.send_json(
                {"type": "error", "payload": {"code": "forbidden"}}
            )
            await websocket.close(code=4403)
            return
        is_op = svc.is_operator(user)

        await websocket.send_json(
            {"type": "auth_ok", "payload": {"user_id": str(user.id)}}
        )
        await ws_manager.connect(ticket_id, websocket)

        # При входе помечаем прочитанными "не свои" сообщения —
        # типичный UX: открыл чат — счётчик непрочитанных обнулился.
        await repo.mark_messages_read(db, ticket_id, is_op)

        # --- MESSAGE LOOP ---
        try:
            while True:
                frame = await websocket.receive_json()
                if frame.get("type") != "message":
                    await websocket.send_json(
                        {"type": "error", "payload": {"code": "bad_frame"}}
                    )
                    continue
                body = (frame.get("body") or "").strip()
                if not body:
                    await websocket.send_json(
                        {"type": "error", "payload": {"code": "empty_body"}}
                    )
                    continue

                msg = await repo.add_message(
                    db,
                    ticket_id=ticket_id,
                    sender_id=user.id,
                    body=body,
                    is_from_operator=is_op,
                )
                # Publish — все инстансы (включая нас) получат через
                # pubsub и разошлют в свои WS.
                await ws_manager.publish(
                    ticket_id,
                    {"type": "message", "payload": _serialize_message(msg)},
                )
        except WebSocketDisconnect:
            # Нормальный разрыв — клиент закрыл вкладку.
            pass
        except Exception:
            logger.exception("WS loop error in ticket %s", ticket_id)
        finally:
            await ws_manager.disconnect(ticket_id, websocket)


# ---------------------------------------------------------------------
# Admin: список всех тикетов
# ---------------------------------------------------------------------


@router.get(
    "/admin/tickets",
    response_model=list[TicketResponse],
    summary="Все тикеты (admin/operator)",
    # require_any_role вместо inline svc.is_operator: проверка
    # происходит на уровне dependency, до запуска тела handler'а.
    # Это декларативнее (один взгляд на сигнатуру = понимание RBAC)
    # и меньше шанс случайно пропустить проверку при копировании
    # эндпоинта.
    dependencies=[Depends(require_any_role("operator", "admin"))],
)
async def list_all_tickets(
    status_: TicketStatus | None = Query(None, alias="status"),
    assigned_to_id: uuid.UUID | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    items = await repo.list_tickets_admin(
        db,
        status=status_,
        assigned_to_id=assigned_to_id,
        page=page,
        per_page=per_page,
    )
    return [TicketResponse.model_validate(t) for t in items]
