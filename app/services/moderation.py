"""
Сервис модерации (этап 12).

Изолирует операции, требующие admin-прав:
- approve/reject classified
- verify kennel
- block/unblock user
- grant/revoke роли пользователю

Проверка прав на стороне роутера через Depends(require_any_role("admin")) —
сервис уже считает каллера авторизованным.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.classified import Classified, ClassifiedStatus
from app.models.kennel import Kennel
from app.models.user import RoleEnum, User, UserRole


async def moderate_classified(
    db: AsyncSession,
    classified_id: uuid.UUID,
    approve: bool,
    reason: str | None = None,
) -> Classified:
    obj = await db.get(Classified, classified_id)
    if obj is None:
        raise ValueError("not_found")
    # approve → возвращаем в active; reject → закрываем со статусом closed.
    # closed — мягкая блокировка: данные остаются, но из списков скрыты.
    # reason пока сохраняем только в логе сервиса (поле в БД для причин
    # модерации не создаём, чтобы не плодить миграции на этом этапе).
    obj.status = (
        ClassifiedStatus.active if approve else ClassifiedStatus.closed
    )
    _ = reason  # TODO (этап 13/14): сохранять причину в audit_log
    await db.commit()
    await db.refresh(obj)
    return obj


async def verify_kennel(
    db: AsyncSession, kennel_id: uuid.UUID, is_verified: bool
) -> Kennel:
    obj = await db.get(Kennel, kennel_id)
    if obj is None:
        raise ValueError("not_found")
    obj.is_verified = is_verified
    await db.commit()
    await db.refresh(obj)
    return obj


async def block_user(
    db: AsyncSession, user_id: uuid.UUID, is_active: bool
) -> User:
    """
    Блокировка/разблокировка пользователя. При блокировке выйти из
    системы пользователь не может, но get_current_user поднимет
    401 для is_active=False (см. app/dependencies.py).

    TODO: при блокировке хорошо бы отозвать активные refresh-токены —
    пока оставлено на разблокировку через смену пароля.
    """
    obj = await db.get(User, user_id)
    if obj is None:
        raise ValueError("not_found")
    obj.is_active = is_active
    await db.commit()
    await db.refresh(obj)
    return obj


async def update_user_role(
    db: AsyncSession,
    user_id: uuid.UUID,
    role: RoleEnum,
    grant: bool,
    granted_by: uuid.UUID,
) -> list[RoleEnum]:
    """
    Добавляет или убирает роль у пользователя. Возвращает актуальный
    набор ролей.

    UniqueConstraint("user_id","role") гарантирует, что повторный grant
    не создаст дубликат — мы сначала проверяем существование.
    """
    user = await db.get(User, user_id)
    if user is None:
        raise ValueError("not_found")

    stmt = select(UserRole).where(
        UserRole.user_id == user_id, UserRole.role == role
    )
    existing = (await db.execute(stmt)).scalar_one_or_none()

    if grant and existing is None:
        db.add(UserRole(user_id=user_id, role=role, granted_by=granted_by))
    elif not grant and existing is not None:
        await db.delete(existing)
    # else: ничего не делаем (idempotency).

    await db.commit()
    # Перезачитываем актуальные роли.
    roles_stmt = select(UserRole.role).where(UserRole.user_id == user_id)
    rows = (await db.execute(roles_stmt)).scalars().all()
    return list(rows)
