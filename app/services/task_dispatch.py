"""
Создание фоновой задачи с гарантированной публикацией (ревью 2026-10-06, BE-19).

Task и outbox-событие с TaskMessage пишутся в ОДНОЙ транзакции —
transactional outbox, как у уведомлений и requeue_stuck_tasks. Раньше
роутеры публиковали сообщение в RabbitMQ напрямую после commit'а задачи:
при недоступном брокере задача навсегда оставалась pending (requeue
подбирает только processing), пользователь видел «генерируется» вечно.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.task import Task
from app.repositories import outbox as outbox_repo
from app.schemas.task import TaskMessage
from app.services.task_queues import QUEUE_FOR_TASK_TYPE


async def create_and_enqueue_task(
    db: AsyncSession,
    *,
    type_: str,
    payload: dict,
    created_by: uuid.UUID | None,
) -> Task:
    """Создать Task(pending) и outbox-событие для его очереди. Коммитит сам."""
    queue = QUEUE_FOR_TASK_TYPE.get(type_)
    if queue is None:
        raise ValueError(f"no queue for task type {type_!r}")
    task = Task(type=type_, payload=payload, created_by=created_by)
    db.add(task)
    await db.flush()
    message = TaskMessage(task_id=task.id, action=type_, payload=payload)
    await outbox_repo.enqueue(
        db,
        exchange=None,  # default exchange: routing_key = имя очереди
        routing_key=queue,
        payload=message.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(task)
    return task
