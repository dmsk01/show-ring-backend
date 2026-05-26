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

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import ModerationLog
from app.models.classified import Classified, ClassifiedStatus
from app.models.kennel import Kennel
from app.models.user import RefreshToken, RoleEnum, User, UserRole


async def _log_action(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID | None,
    action: str,
    target_type: str,
    target_id: uuid.UUID,
    reason: str | None = None,
    extra: dict | None = None,
) -> None:
    """
    Запись в moderation_logs. Без COMMIT — вызывающий сервис делает
    свой commit одной транзакцией с основным изменением. Так лог
    появляется ↔ действие выполнено, никаких "потерянных" логов.
    """
    db.add(
        ModerationLog(
            actor_id=actor_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            reason=reason,
            extra=extra,
        )
    )


async def moderate_classified(
    db: AsyncSession,
    classified_id: uuid.UUID,
    approve: bool,
    reason: str | None = None,
    *,
    actor_id: uuid.UUID | None = None,
) -> Classified:
    obj = await db.get(Classified, classified_id)
    if obj is None:
        raise ValueError("not_found")
    prev_status = obj.status
    # approve → возвращаем в active; reject → closed (мягкая блокировка,
    # данные сохранены).
    obj.status = (
        ClassifiedStatus.active if approve else ClassifiedStatus.closed
    )
    await _log_action(
        db,
        actor_id=actor_id,
        action="classified.approve" if approve else "classified.reject",
        target_type="classified",
        target_id=classified_id,
        reason=reason,
        extra={
            "prev_status": prev_status.value,
            "new_status": obj.status.value,
        },
    )
    await db.commit()
    await db.refresh(obj)
    return obj


async def verify_kennel(
    db: AsyncSession,
    kennel_id: uuid.UUID,
    is_verified: bool,
    *,
    actor_id: uuid.UUID | None = None,
) -> Kennel:
    obj = await db.get(Kennel, kennel_id)
    if obj is None:
        raise ValueError("not_found")
    obj.is_verified = is_verified
    await _log_action(
        db,
        actor_id=actor_id,
        action="kennel.verify" if is_verified else "kennel.unverify",
        target_type="kennel",
        target_id=kennel_id,
    )
    await db.commit()
    await db.refresh(obj)
    return obj


async def block_user(
    db: AsyncSession,
    user_id: uuid.UUID,
    is_active: bool,
    *,
    actor_id: uuid.UUID | None = None,
    reason: str | None = None,
) -> User:
    """
    Блокировка/разблокировка пользователя. При блокировке:
    1. is_active=False → get_current_user поднимет 401 на следующем
       запросе с access-токеном.
    2. Все активные refresh-токены пользователя помечаются revoked —
       без этого старая мобильная сессия могла бы рефрешнуться и
       получить новый access-токен (хоть тот сразу упал бы на
       is_active-проверке, всё равно лишний шум).

    Разблокировка (is_active=True) refresh'ы НЕ возвращает — это будет
    «оживлением сессий», пользователь должен залогиниться заново.
    """
    obj = await db.get(User, user_id)
    if obj is None:
        raise ValueError("not_found")
    obj.is_active = is_active
    if not is_active:
        # Отзываем все живые refresh-токены пользователя одним UPDATE.
        # is_revoked=true вместо DELETE — для аудита: после восстановления
        # можно посмотреть, когда и сколько токенов было.
        await db.execute(
            update(RefreshToken)
            .where(
                RefreshToken.user_id == user_id,
                RefreshToken.is_revoked.is_(False),
            )
            .values(is_revoked=True)
        )
    await _log_action(
        db,
        actor_id=actor_id,
        action="user.block" if not is_active else "user.unblock",
        target_type="user",
        target_id=user_id,
        reason=reason,
    )
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

    await _log_action(
        db,
        actor_id=granted_by,
        action="user.role_grant" if grant else "user.role_revoke",
        target_type="user",
        target_id=user_id,
        extra={"role": role.value},
    )
    await db.commit()
    # Перезачитываем актуальные роли.
    roles_stmt = select(UserRole.role).where(UserRole.user_id == user_id)
    rows = (await db.execute(roles_stmt)).scalars().all()
    return list(rows)
