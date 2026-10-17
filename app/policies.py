"""
Политики доступа предметной области (ревью 2026-10-06, BE-33).

Одно место для правил «кто может что», которые раньше копировались по
сервисам и расходились (review 2026-06-10: владелец собаки без питомника не
мог редактировать собственную карточку — правило обновили в одном месте,
но не в другом).

Actor — кто выполняет действие. Раньше сервисы принимали пару
(requester_id, is_admin), а роутеры вычисляли is_admin каждый своей копией
_is_admin(user): появись новая роль с правами админа — править пришлось бы
десяток мест. Теперь роутер передаёт Actor.of(user), а «кто такой admin»
решается здесь.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dog import Dog
from app.repositories import kennel as kennel_repo

if TYPE_CHECKING:
    from app.models.user import User


@dataclass(frozen=True, slots=True)
class Actor:
    """Пользователь, от имени которого выполняется операция."""

    user_id: uuid.UUID
    roles: frozenset[str] = frozenset()

    @classmethod
    def of(cls, user: User) -> Actor:
        return cls(user_id=user.id, roles=frozenset(r.role.value for r in user.roles))

    @property
    def is_admin(self) -> bool:
        return "admin" in self.roles

    def has_role(self, *roles: str) -> bool:
        """Есть хотя бы одна из ролей. admin проходит любую проверку роли."""
        return self.is_admin or any(r in self.roles for r in roles)

    def owns(self, owner_id: uuid.UUID | None) -> bool:
        """Объект принадлежит этому пользователю."""
        return owner_id is not None and owner_id == self.user_id


async def can_manage_dog(db: AsyncSession, dog: Dog, actor: Actor) -> bool:
    """
    Управлять собакой (редактировать, удалять, записывать на выставку) может
    её владелец (Dog.owner_id), владелец её питомника или admin. Питомник
    управляет своими собаками и при заданном owner_id (review 2026-06-10 —
    поведение признано намеренным).
    """
    if actor.is_admin or actor.owns(dog.owner_id):
        return True
    if dog.kennel_id is None:
        return False
    kennel = await kennel_repo.get_kennel(db, dog.kennel_id)
    return kennel is not None and actor.owns(kennel.owner_id)
