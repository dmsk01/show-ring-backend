"""
Схемы поддержки (этап 11).

REST-схемы для тикетов/сообщений + контракт WebSocket-кадров
(WSAuthFrame / WSMessageFrame).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.support import TicketPriority, TicketStatus


# ---------------------------------------------------------------------
# Tickets
# ---------------------------------------------------------------------


class TicketCreate(BaseModel):
    subject: str = Field(..., min_length=3, max_length=255)
    body: str = Field(..., min_length=3)
    priority: TicketPriority = TicketPriority.normal


class TicketStatusUpdate(BaseModel):
    status: TicketStatus


class TicketAssignRequest(BaseModel):
    assigned_to_id: uuid.UUID | None


class TicketResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    subject: str
    status: TicketStatus
    priority: TicketPriority
    assigned_to_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------


class MessageCreate(BaseModel):
    body: str = Field(..., min_length=1, max_length=10_000)


class MessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    ticket_id: uuid.UUID
    sender_id: uuid.UUID | None
    is_from_operator: bool
    body: str
    is_read: bool
    created_at: datetime


# ---------------------------------------------------------------------
# WebSocket frames
# ---------------------------------------------------------------------
# Раздельные классы вместо одного с полем "type" — pydantic умеет
# валидировать дискриминированные union'ы; на этапе 11 проще завести
# отдельные модели и разбирать вручную через .get("type").


class WSAuthFrame(BaseModel):
    """
    Первый кадр от клиента после WS-handshake. Содержит JWT — НЕ в URL
    (URL логируется в reverse-proxy / browser history, выйдет утечка).
    """

    type: Literal["auth"] = "auth"
    token: str


class WSMessageFrame(BaseModel):
    """
    Кадр текстового сообщения. body — содержимое.
    """

    type: Literal["message"] = "message"
    body: str = Field(..., min_length=1, max_length=10_000)


class WSOutboundFrame(BaseModel):
    """
    Кадр, который сервер отправляет клиенту. Унифицирован для
    'auth_ok' / 'message' / 'error'.
    """

    type: str
    # Гибкое поле под разные кадры. Например:
    # type=message → {id, body, sender_id, is_from_operator, created_at}
    # type=error   → {code, detail}
    payload: dict = Field(default_factory=dict)
