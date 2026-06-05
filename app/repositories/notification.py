"""
Репозиторий подписок и уведомлений (этап 9).

Главный запрос здесь — `find_subscribers`: для входящего события
с известным event_type и (опционально) breed_id/region вернуть
подписки + email-адреса юзеров для отправки уведомлений.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationStatus,
    Subscription,
)
from app.models.user import User

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------


async def create_subscription(
    db: AsyncSession, **fields
) -> Subscription:
    obj = Subscription(**fields)
    db.add(obj)
    await db.flush()
    await db.commit()
    await db.refresh(obj)
    return obj


async def get_subscription(
    db: AsyncSession, id_: uuid.UUID
) -> Subscription | None:
    return await db.get(Subscription, id_)


async def list_user_subscriptions(
    db: AsyncSession, user_id: uuid.UUID
) -> Sequence[Subscription]:
    stmt = (
        select(Subscription)
        .where(Subscription.user_id == user_id)
        .order_by(Subscription.created_at.desc())
    )
    return (await db.execute(stmt)).scalars().all()


async def delete_subscription(
    db: AsyncSession, sub: Subscription
) -> None:
    await db.delete(sub)
    await db.commit()


async def find_subscribers(
    db: AsyncSession,
    *,
    event_type: str,
    breed_id: uuid.UUID | None = None,
    region: str | None = None,
    channel: NotificationChannel = NotificationChannel.email,
    exclude_user_id: uuid.UUID | None = None,
) -> Sequence[tuple[Subscription, User]]:
    """
    Возвращает (Subscription, User) для всех подписчиков, чья подписка
    подходит под событие.

    Семантика фильтров: NULL в подписке = "любой". То есть подписка
    без filter_breed_id матчится на любой breed. Если же в подписке
    filter_breed_id задан — он должен совпадать с приходящим event.

    exclude_user_id — не уведомлять автора события (типичный кейс:
    заводчик опубликовал помёт — ему самому письмо не шлём).
    """
    stmt = (
        select(Subscription, User)
        .join(User, User.id == Subscription.user_id)
        .where(
            Subscription.event_type == event_type,
            Subscription.channel == channel,
            Subscription.is_active.is_(True),
            User.is_active.is_(True),
        )
    )

    # Фильтр по породе: либо подписка без породы (любая), либо точное
    # совпадение с приходящим event'ом. В SQL это OR с NULL-check'ом.
    if breed_id is not None:
        stmt = stmt.where(
            (Subscription.filter_breed_id.is_(None))
            | (Subscription.filter_breed_id == breed_id)
        )
    else:
        # Если у события нет breed_id, считаем что подписки с явным
        # фильтром по породе не подходят — иначе пришлось бы рассылать
        # "все события всем", что некорректно по семантике.
        stmt = stmt.where(Subscription.filter_breed_id.is_(None))

    if region is not None:
        # Region — точное сопоставление (без учёта регистра).
        stmt = stmt.where(
            (Subscription.filter_region.is_(None))
            | (Subscription.filter_region.ilike(region))
        )
    else:
        stmt = stmt.where(Subscription.filter_region.is_(None))

    if exclude_user_id is not None:
        stmt = stmt.where(User.id != exclude_user_id)

    rows = (await db.execute(stmt)).all()
    return [(row[0], row[1]) for row in rows]


# ---------------------------------------------------------------------
# Notifications (log)
# ---------------------------------------------------------------------


async def create_notification(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    event_type: str,
    channel: NotificationChannel,
    subject: str,
) -> Notification:
    """
    Создаёт запись лога со status=pending. После отправки worker
    обновит на sent/failed через mark_sent / mark_failed.
    """
    obj = Notification(
        user_id=user_id,
        event_type=event_type,
        channel=channel,
        subject=subject,
        status=NotificationStatus.pending,
    )
    db.add(obj)
    await db.flush()
    await db.commit()
    await db.refresh(obj)
    return obj


async def list_user_notifications(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    channel: NotificationChannel | None = None,
    page: int = 1,
    per_page: int = 50,
) -> Sequence[Notification]:
    # channel (этап 16): необязательный фильтр. Колокольчик передаёт
    # in_app, чтобы не смешивать realtime-ленту с журналом email —
    # дедуп между каналами решается этим фильтром by design.
    stmt = select(Notification).where(Notification.user_id == user_id)
    if channel is not None:
        stmt = stmt.where(Notification.channel == channel)
    stmt = (
        stmt.order_by(Notification.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    return (await db.execute(stmt)).scalars().all()


async def count_unread_notifications(
    db: AsyncSession, user_id: uuid.UUID
) -> int:
    """Число непрочитанных уведомлений пользователя (read_at IS NULL)."""
    stmt = (
        select(func.count())
        .select_from(Notification)
        .where(
            Notification.user_id == user_id,
            Notification.read_at.is_(None),
        )
    )
    return (await db.execute(stmt)).scalar_one()


async def mark_all_notifications_read(
    db: AsyncSession, user_id: uuid.UUID
) -> int:
    """
    Помечает все непрочитанные уведомления пользователя прочитанными
    одним атомарным UPDATE. Возвращает число затронутых строк (сколько
    реально пометили) — фронт может показать «отмечено N».
    """
    stmt = (
        update(Notification)
        .where(
            Notification.user_id == user_id,
            Notification.read_at.is_(None),
        )
        .values(read_at=datetime.now(timezone.utc))
    )
    result = await db.execute(stmt)
    await db.commit()
    return getattr(result, "rowcount", 0)


# Образцы для dev-сидера моков (см. create_mock_notifications).
_MOCK_NOTIFICATIONS: list[tuple[str, str]] = [
    ("dog.title_earned", "Ваша собака получила титул CW"),
    ("show.registration_opened", "Открыта регистрация на выставку «Весна-2026»"),
    ("show.results_published", "Опубликованы результаты выставки"),
    ("litter.announced", "Новый помёт у питомника, на который вы подписаны"),
]


async def create_mock_notifications(
    db: AsyncSession, user_id: uuid.UUID, count: int
) -> list[Notification]:
    """
    Создаёт count моковых уведомлений (все непрочитанные) для отладки UI.
    Используется только dev-эндпоинтом под guard'ом settings.debug.
    """
    items: list[Notification] = []
    for i in range(count):
        event_type, subject = _MOCK_NOTIFICATIONS[i % len(_MOCK_NOTIFICATIONS)]
        n = Notification(
            user_id=user_id,
            event_type=event_type,
            channel=NotificationChannel.email,
            subject=f"{subject} (#{i + 1})",
            status=NotificationStatus.sent,
            sent_at=datetime.now(timezone.utc),
        )
        db.add(n)
        items.append(n)
    await db.flush()
    await db.commit()
    for n in items:
        await db.refresh(n)  # подтянуть server_default created_at для ответа
    return items


async def mark_notification_read(
    db: AsyncSession, notification_id: uuid.UUID, user_id: uuid.UUID
) -> Notification | None:
    """
    Помечает уведомление прочитанным (read_at = now), если оно
    принадлежит user_id. Возвращает обновлённый Notification — или None,
    если записи нет ЛИБО она чужая (роутер отдаёт 404, не раскрывая
    существование чужого уведомления — IDOR-safe по конструкции).

    Идемпотентно: если read_at уже проставлен, повторный вызов его не
    меняет и не делает лишний commit — просто возвращает запись. Гонка
    двух параллельных «прочитать» безобидна (оба ставят ~один и тот же
    момент), поэтому здесь достаточно ORM-пути без атомарного UPDATE.
    """
    n = await db.get(Notification, notification_id)
    if n is None or n.user_id != user_id:
        return None
    if n.read_at is None:
        n.read_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(n)
    return n


async def mark_sent(db: AsyncSession, notification_id: uuid.UUID) -> bool:
    """
    Перевод уведомления в sent. Возвращает True, если запись обновлена,
    False — если notification с таким id отсутствует (битый UUID,
    Notification удалён cleanup-джобом).

    ИСПРАВЛЕНО (bug_241 audit 2026-05-28): без rowcount-check'а UPDATE
    с неверным id молча возвращал 0 изменённых строк — никаких ошибок,
    но и эффекта тоже. Это маскировало битые сообщения email_handler'а.
    """
    stmt = (
        update(Notification)
        .where(Notification.id == notification_id)
        .values(status=NotificationStatus.sent, sent_at=datetime.now(timezone.utc))
    )
    result = await db.execute(stmt)
    await db.commit()
    rowcount = getattr(result, "rowcount", 0)
    if rowcount == 0:
        logger.warning(
            "mark_sent: notification %s not found (rowcount=0)",
            notification_id,
        )
    return rowcount == 1


async def mark_failed(
    db: AsyncSession, notification_id: uuid.UUID, error: str
) -> bool:
    """
    Перевод в failed с текстом ошибки. См. mark_sent — та же семантика
    возврата bool (bug_241 audit 2026-05-28).
    """
    stmt = (
        update(Notification)
        .where(Notification.id == notification_id)
        .values(status=NotificationStatus.failed, error=error[:2000])
    )
    result = await db.execute(stmt)
    await db.commit()
    rowcount = getattr(result, "rowcount", 0)
    if rowcount == 0:
        logger.warning(
            "mark_failed: notification %s not found (rowcount=0)",
            notification_id,
        )
    return rowcount == 1
