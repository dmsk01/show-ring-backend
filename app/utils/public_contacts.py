"""
Скрытие контактов без согласия на распространение (ст. 10.1 152-ФЗ).

Контакты питомника/объявления — персональные данные владельца. Посторонним
они отдаются только при contacts_public=True (согласие дано переключателем,
факт — в user_consents). Владелец и админ видят контакты всегда: иначе
форма редактирования получила бы пустые поля и затёрла их при сохранении.
"""

from __future__ import annotations

import uuid
from typing import TypeVar

from pydantic import BaseModel

from app.dependencies import is_admin
from app.models.user import User

T = TypeVar("T", bound=BaseModel)

CONTACT_FIELDS: tuple[str, ...] = ("contact_phone", "contact_email")


def hide_private_contacts(
    resp: T,
    *,
    owner_id: uuid.UUID,
    contacts_public: bool,
    viewer: User | None,
    fields: tuple[str, ...] = CONTACT_FIELDS,
) -> T:
    if contacts_public:
        return resp
    if viewer is not None and (viewer.id == owner_id or is_admin(viewer)):
        return resp
    return resp.model_copy(update={f: None for f in fields})
