"""
Интеграция: задачи документов и изображений публикуются через outbox
(ревью 2026-10-06, BE-19).

Раньше задача создавалась в БД, а сообщение публиковалось в RabbitMQ
напрямую. Если брокер был недоступен, задача навсегда оставалась pending:
requeue_stuck_tasks подбирает только processing, а обещанного «перепубликуем
позже» не существовало. Теперь Task и outbox-событие — одна транзакция.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from sqlalchemy import select

from app.models.outbox import OutboxEvent
from app.models.task import Task, TaskStatusEnum
from app.services import scheduler
from app.services.rabbit import rabbit_service
from app.services.task_dispatch import create_and_enqueue_task
from app.services.task_queues import DOCUMENT_TASK_QUEUE, IMAGE_TASK_QUEUE, IMAGE_TASK_TYPE


async def _outbox_for(db_session, task_id) -> list[OutboxEvent]:
    rows = (await db_session.execute(select(OutboxEvent))).scalars().all()
    return [r for r in rows if r.payload.get("task_id") == str(task_id)]


async def test_document_task_goes_through_outbox(db_session, monkeypatch):
    monkeypatch.setattr(
        rabbit_service, "publish", AsyncMock(side_effect=RuntimeError("broker down"))
    )
    task = await create_and_enqueue_task(
        db_session, type_="generate_catalog", payload={"show_id": str(uuid.uuid4())},
        created_by=None,
    )
    assert task.status == TaskStatusEnum.pending
    events = await _outbox_for(db_session, task.id)
    assert len(events) == 1
    assert events[0].routing_key == DOCUMENT_TASK_QUEUE
    assert events[0].payload["action"] == "generate_catalog"
    rabbit_service.publish.assert_not_called()


async def test_image_task_goes_to_image_queue(db_session):
    task = await create_and_enqueue_task(
        db_session, type_=IMAGE_TASK_TYPE, payload={"file_id": str(uuid.uuid4())},
        created_by=None,
    )
    events = await _outbox_for(db_session, task.id)
    assert [e.routing_key for e in events] == [IMAGE_TASK_QUEUE]


async def test_requeue_marks_unknown_task_type_failed(db_session, monkeypatch):
    # Раньше статус ставился в pending ДО проверки очереди, и задача без
    # очереди навсегда зависала в pending без сообщения.
    task = Task(type="unknown_kind", payload={}, status=TaskStatusEnum.processing)
    db_session.add(task)
    await db_session.commit()
    task.updated_at = datetime.now(timezone.utc) - timedelta(hours=2)
    await db_session.commit()

    class _Factory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *exc):
            return False

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _lock(job_name, ttl_seconds=300):
        yield True

    monkeypatch.setattr(scheduler, "async_session_factory", _Factory())
    monkeypatch.setattr(scheduler, "_scheduler_lock", _lock)
    await scheduler.requeue_stuck_tasks()

    await db_session.refresh(task)
    assert task.status == TaskStatusEnum.failed
