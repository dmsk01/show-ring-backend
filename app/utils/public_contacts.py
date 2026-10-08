"""
Контакты питомника/объявления в публичных ответах.

Два слоя защиты:

1. Согласие на распространение (ст. 10.1 152-ФЗ). Контакты — персональные
   данные владельца; посторонним они отдаются только при contacts_public=True
   (согласие дано переключателем, факт — в user_consents).
2. Защита от сборщиков (план защиты 2026-10-05, этап 4). Даже открытые
   контакты не кладём в списки и карточки — бот, обходящий витрину, собрал
   бы все телефоны разом. В ответе только признак has_public_contacts, сами
   контакты — отдельным запросом по кнопке «Показать контакты»
   (GET /…/{id}/contacts) с лимитом на IP.

Владелец и админ видят контакты в карточке всегда: иначе форма
редактирования получила бы пустые поля и затёрла их при сохранении.
"""

from __future__ import annotations

import uuid
from typing import TypeVar

from pydantic import BaseModel

from app.dependencies import is_admin
from app.models.user import User

T = TypeVar("T", bound=BaseModel)

CONTACT_FIELDS: tuple[str, ...] = ("contact_phone", "contact_email")

# Раскрытий контактов с одного IP в час — с запасом для человека,
# листающего объявления, и мало для сборщика базы.
REVEAL_LIMIT_PER_HOUR = 30


def can_see_contacts(viewer: User | None, owner_id: uuid.UUID) -> bool:
    return viewer is not None and (viewer.id == owner_id or is_admin(viewer))


def hide_private_contacts(
    resp: T,
    *,
    owner_id: uuid.UUID,
    contacts_public: bool,
    viewer: User | None,
    fields: tuple[str, ...] = CONTACT_FIELDS,
) -> T:
    """Убрать контакты из ответа для постороннего, оставив признак их наличия."""
    has_any = any(getattr(resp, f, None) for f in fields)
    has_public = contacts_public and has_any
    if can_see_contacts(viewer, owner_id):
        return resp.model_copy(update={"has_public_contacts": has_public})
    return resp.model_copy(
        update={**{f: None for f in fields}, "has_public_contacts": has_public}
    )


def contacts_or_none(
    obj,
    *,
    viewer: User | None,
    fields: tuple[str, ...] = CONTACT_FIELDS,
) -> dict | None:
    """
    Контакты для «Показать контакты»: dict полей или None (→ 404), если
    согласия на распространение нет или контактов нет вовсе. Владельцу и
    админу — всегда.
    """
    owner_id = getattr(obj, "owner_id", None) or getattr(obj, "author_id")
    allowed = obj.contacts_public or can_see_contacts(viewer, owner_id)
    data = {f: getattr(obj, f) for f in fields}
    if not allowed or not any(data.values()):
        return None
    return data
