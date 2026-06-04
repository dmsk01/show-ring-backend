"""Репозиторий аудит-лога чувствительных операций (этап 19)."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.security_audit import SecurityAuditLog


async def record_security_event(
    db: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    action: str,
    ip: str | None = None,
    user_agent: str | None = None,
    extra: dict | None = None,
) -> SecurityAuditLog:
    """
    Записать событие безопасности. БЕЗ commit — пишется в той же
    транзакции, что и сама операция (атомарность «сделали ⇔ записали»).
    user_agent обрезаем до лимита колонки.
    """
    event = SecurityAuditLog(
        user_id=user_id,
        action=action,
        ip=ip,
        user_agent=user_agent[:512] if user_agent else None,
        extra=extra,
    )
    db.add(event)
    await db.flush()
    return event
