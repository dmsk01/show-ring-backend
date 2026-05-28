"""
Репозиторий фоновых задач (этап 8).

Здесь же — оптимистическая блокировка для перехода статусов. Это даёт
воркеру гарантию, что задача не будет обработана дважды, даже если
запущено несколько инстансов worker'а.
"""

from __future__ import annotations

import uuid

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.task import Task, TaskStatusEnum


async def get_task(db: AsyncSession, task_id: uuid.UUID) -> Task | None:
    return await db.get(Task, task_id)


async def create_task(
    db: AsyncSession,
    *,
    type_: str,
    payload: dict,
    created_by: uuid.UUID | None = None,
) -> Task:
    """
    Создаёт задачу в БД. Сохраняем под type_ (а не type), чтобы не
    шадоить встроенный type() — pyright такое снимает, но привычка вредная.
    """
    obj = Task(type=type_, payload=payload, created_by=created_by)
    db.add(obj)
    await db.flush()
    await db.commit()
    await db.refresh(obj)
    return obj


async def claim_task(
    db: AsyncSession, task_id: uuid.UUID
) -> bool:
    """
    Оптимистический "захват" задачи воркером: pending → processing.

    Возвращает True, если статус успешно обновлён (задача "наша");
    False — если кто-то уже её забрал (status != pending).

    Реализация через UPDATE … WHERE status='pending'. PostgreSQL
    атомарно проверит условие и сделает UPDATE — никакой race condition
    между чтением и записью. rowcount=1 значит "успели первыми".
    """
    stmt = (
        update(Task)
        .where(Task.id == task_id, Task.status == TaskStatusEnum.pending)
        .values(status=TaskStatusEnum.processing, attempts=Task.attempts + 1)
    )
    result = await db.execute(stmt)
    await db.commit()
    # SQLAlchemy 2.0 для UPDATE возвращает CursorResult с .rowcount,
    # но AsyncSession.execute аннотирован как Result[Any], где этого
    # атрибута нет. getattr — мостик через generic-тип без cast'а.
    return getattr(result, "rowcount", 0) == 1


async def mark_done(
    db: AsyncSession, task_id: uuid.UUID, result_payload: dict
) -> bool:
    """
    Перевод в терминальный статус done. result сохраняем целиком.

    ИСПРАВЛЕНО (bug_233 audit 2026-05-28): UPDATE с условием
    `status = processing` — только воркер, удержавший claim, может
    перевести задачу в done. Если два воркера случайно подхватили
    одну (split-brain pool/двойной consume) — второй mark_done
    отлетит без затирания результата первого. Возвращаем bool, чтобы
    воркер мог залогировать warning при rowcount=0.
    """
    stmt = (
        update(Task)
        .where(
            Task.id == task_id,
            Task.status == TaskStatusEnum.processing,
        )
        .values(status=TaskStatusEnum.done, result=result_payload)
    )
    result = await db.execute(stmt)
    await db.commit()
    return getattr(result, "rowcount", 0) == 1


async def mark_failed(
    db: AsyncSession, task_id: uuid.UUID, error: str
) -> bool:
    """
    Перевод в failed. error усечён до 2000 символов — длинные стектрейсы
    лучше писать в логи, а в БД хранить причину.

    ИСПРАВЛЕНО (bug_233 audit 2026-05-28): такой же гард по
    `status = processing`, что и в mark_done — иначе fail с retry
    мог затереть уже успешный результат другого воркера.
    """
    stmt = (
        update(Task)
        .where(
            Task.id == task_id,
            Task.status == TaskStatusEnum.processing,
        )
        .values(
            status=TaskStatusEnum.failed,
            result={"error": error[:2000]},
        )
    )
    result = await db.execute(stmt)
    await db.commit()
    return getattr(result, "rowcount", 0) == 1
