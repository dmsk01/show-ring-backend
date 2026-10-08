"""
Сроки хранения персональных данных (Политика конфиденциальности, раздел 11).

По достижении цели обработки данные уничтожаются или обезличиваются
(ч. 4 ст. 21 152-ФЗ). Здесь — то, что не уходит вместе с аккаунтом и
копится само по себе:

- security_audit_logs (IP, User-Agent, старый/новый email) — цель
  «расследование угонов» достигается за год → удаляем;
- ad_events (IP, хэш User-Agent, user_id) — антифрод и сверка с
  рекламодателем укладываются в полгода → обезличиваем, само событие
  остаётся: агрегаты показов/кликов нужны для отчётности;
- outbox_events (ревью 2026-10-06, BE-11) — payload email-задач содержит
  адрес получателя и HTML письма со ссылками-токенами. Цель (доставка)
  достигнута → sent удаляем через 7 дней (запас на разбор инцидентов),
  failed — через 30 дней (срок уничтожения по ч. 4 ст. 21 152-ФЗ).

Сроки менять только вместе с текстом Политики.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import and_, delete, or_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ad import AdEvent
from app.models.outbox import OutboxEvent, OutboxStatus
from app.models.security_audit import SecurityAuditLog

SECURITY_LOG_RETENTION = timedelta(days=365)
AD_EVENT_PII_RETENTION = timedelta(days=183)
OUTBOX_SENT_RETENTION = timedelta(days=7)
OUTBOX_FAILED_RETENTION = timedelta(days=30)


async def purge_expired_personal_data(
    db: AsyncSession, *, now: datetime
) -> dict[str, int]:
    """Удалить/обезличить данные с истёкшим сроком. Коммитит сам."""
    logs = await db.execute(
        delete(SecurityAuditLog).where(
            SecurityAuditLog.created_at < now - SECURITY_LOG_RETENTION
        )
    )
    events = await db.execute(
        update(AdEvent)
        .where(
            AdEvent.created_at < now - AD_EVENT_PII_RETENTION,
            or_(
                AdEvent.ip.is_not(None),
                AdEvent.user_agent_hash.is_not(None),
                AdEvent.user_id.is_not(None),
            ),
        )
        .values(ip=None, user_agent_hash=None, user_id=None)
    )
    outbox = await db.execute(
        delete(OutboxEvent).where(
            or_(
                and_(
                    OutboxEvent.status == OutboxStatus.sent,
                    OutboxEvent.created_at < now - OUTBOX_SENT_RETENTION,
                ),
                and_(
                    OutboxEvent.status == OutboxStatus.failed,
                    OutboxEvent.created_at < now - OUTBOX_FAILED_RETENTION,
                ),
            )
        )
    )
    await db.commit()
    # rowcount есть у CursorResult (DML), но не в типе Result — как в
    # services/scheduler.py, читаем через getattr.
    return {
        "security_logs_deleted": getattr(logs, "rowcount", 0) or 0,
        "ad_events_anonymized": getattr(events, "rowcount", 0) or 0,
        "outbox_events_deleted": getattr(outbox, "rowcount", 0) or 0,
    }
