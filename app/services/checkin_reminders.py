"""
Напоминание о недостающих/просроченных документах за 3 дня до выставки.

Почему не publish_event: события проекта рассылаются по ПОДПИСКАМ
(events_handler ищет подписчиков), а здесь адресат конкретный — тот,
кто записал собаку. Поэтому как transactional-письма: in_app
Notification + письмо через outbox + WS-push.

Идемпотентность: message_id in_app-строки детерминирован
(uuid5 от выставки и получателя) + UNIQUE в notifications. Повторный
запуск (рестарт, вторая реплика) упадёт на IntegrityError, и вся
транзакция — включая письмо — откатится.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import redis as redis_state
from app.config import settings
from app.models.dog import Dog
from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationStatus,
)
from app.models.reference import ShowClass
from app.models.show import Show, ShowEntry, ShowStatus
from app.models.user import User
from app.repositories import checkin as checkin_repo
from app.schemas.notification import NotificationResponse
from app.services import checkin_rules as rules
from app.services.email import render_email
from app.services.email_tasks import enqueue_transactional_email

logger = logging.getLogger(__name__)

EVENT_TYPE = "show.documents_missing"
DAYS_AHEAD = 3


@dataclass
class ReminderItem:
    dog_name: str
    problems: list[str]


@dataclass
class Reminder:
    user_id: uuid.UUID
    show: Show
    items: list[ReminderItem] = field(default_factory=list)


async def collect_document_reminders(db: AsyncSession, today: date) -> list[Reminder]:
    target = today + timedelta(days=DAYS_AHEAD)
    shows = (
        await db.execute(
            select(Show).where(
                Show.checkin_enabled.is_(True),
                Show.status.in_((ShowStatus.registration_open, ShowStatus.registration_closed)),
                Show.date_start == target,
            )
        )
    ).scalars().all()
    reminders: list[Reminder] = []
    for show in shows:
        rows = (
            await db.execute(
                select(ShowEntry, Dog, ShowClass)
                .join(Dog, Dog.id == ShowEntry.dog_id)
                .join(ShowClass, ShowClass.id == ShowEntry.show_class_id)
                .where(ShowEntry.show_id == show.id)
                .order_by(ShowEntry.created_at)
            )
        ).all()
        docs = await checkin_repo.list_dog_documents(db, {dog.id for _, dog, _ in rows})
        ref = rules.reference_date(show.date_start, show.date_end)
        by_user: dict[uuid.UUID, Reminder] = {}
        for entry, dog, cls in rows:
            current = rules.current_documents([d for d, _ in docs if d.dog_id == dog.id])
            problems = rules.document_problems(current, ref, cls.code)
            if not problems:
                continue
            reminder = by_user.setdefault(
                entry.registered_by, Reminder(entry.registered_by, show)
            )
            reminder.items.append(ReminderItem(dog.name, problems))
        reminders.extend(by_user.values())
    return reminders


async def _push(user_id: uuid.UUID, notif: Notification) -> None:
    client = redis_state.redis_client
    if client is None:
        return
    payload = NotificationResponse.model_validate(notif).model_dump(mode="json")
    try:
        await client.publish(
            f"notif:{user_id}", json.dumps({"type": "notification", "payload": payload})
        )
    except Exception as e:  # noqa: BLE001 — push best-effort, строка уже в БД
        logger.warning("documents reminder push failed for %s: %s", user_id, e)


async def _already_sent(db: AsyncSession, message_id: uuid.UUID) -> bool:
    stmt = select(Notification.id).where(Notification.message_id == message_id)
    return (await db.execute(stmt)).first() is not None


async def send_document_reminders(db: AsyncSession, today: date) -> int:
    sent = 0
    for r in await collect_document_reminders(db, today):
        user = await db.get(User, r.user_id)
        if user is None:
            continue
        context = {
            "show_name": r.show.name,
            "date_start": r.show.date_start.strftime("%d.%m.%Y"),
            "ticket_url": f"{settings.frontend_base_url}/dashboard/my-shows/{r.show.id}/ticket",
            "dogs": [
                {"name": i.dog_name, "problems": [rules.PROBLEM_LABELS[p] for p in i.problems]}
                for i in r.items
            ],
        }
        message_id = uuid.uuid5(uuid.NAMESPACE_OID, f"{EVENT_TYPE}:{r.show.id}:{user.id}")
        # Обычный путь повторного запуска — явная проверка. Без rollback
        # всей сессии: он «протухал» бы объекты следующих напоминаний.
        if await _already_sent(db, message_id):
            continue
        subject, _html, _text = render_email(EVENT_TYPE, context)
        notif = Notification(
            user_id=user.id,
            event_type=EVENT_TYPE,
            channel=NotificationChannel.in_app,
            subject=subject,
            status=NotificationStatus.sent,
            sent_at=datetime.now(timezone.utc),
            message_id=message_id,
        )
        try:
            # SAVEPOINT: гонка двух реплик упадёт на UNIQUE message_id и
            # откатит только своё напоминание (вместе с письмом).
            async with db.begin_nested():
                db.add(notif)
                await db.flush()
                if user.email:
                    await enqueue_transactional_email(
                        db, user_id=user.id, to_email=user.email,
                        template_name=EVENT_TYPE, context=context,
                    )
            await db.commit()
        except IntegrityError:
            continue
        await db.refresh(notif)
        await _push(user.id, notif)
        sent += 1
    return sent
