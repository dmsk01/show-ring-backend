"""
Политики доступа предметной области (ревью 2026-10-06, BE-33).

Одно место для правил «кто может что», которые раньше копировались по
сервисам и расходились (review 2026-06-10: владелец собаки без питомника не
мог редактировать собственную карточку — правило обновили в одном месте,
но не в другом).
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dog import Dog
from app.repositories import kennel as kennel_repo


async def can_manage_dog(
    db: AsyncSession, dog: Dog, user_id: uuid.UUID, *, is_admin: bool
) -> bool:
    """
    Управлять собакой (редактировать, удалять, записывать на выставку) может
    её владелец (Dog.owner_id), владелец её питомника или admin. Питомник
    управляет своими собаками и при заданном owner_id (review 2026-06-10 —
    поведение признано намеренным).
    """
    if is_admin or (dog.owner_id is not None and dog.owner_id == user_id):
        return True
    if dog.kennel_id is None:
        return False
    kennel = await kennel_repo.get_kennel(db, dog.kennel_id)
    return kennel is not None and kennel.owner_id == user_id
