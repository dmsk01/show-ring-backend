"""
Модель фоновой задачи (этап 8).

Архитектура. Task представляет работу, отправленную в RabbitMQ:
1. API создаёт запись в БД со status=pending.
2. Публикует сообщение в очередь с task_id.
3. Воркер обновляет статус: pending → processing → done/failed.
4. Результат (file_id PDF в MinIO или сообщение об ошибке) сохраняется
   в JSONB-поле result.

Почему в БД, а не in-memory:
- Переживает рестарт API и воркера. Если воркер перезапустился во
  время обработки — задача всё ещё в pending/processing, можно
  переотправить.
- Несколько инстансов API могут читать статус, не теряя данные.
- История задач сохраняется для отладки и аудита.

Решения:
- payload и result как JSONB — гибкая схема под разные типы задач
  (catalog, diploma, certificate). Лучше, чем создавать колонки
  под каждый тип.
- type как String, а не Enum — простота расширения без миграции
  при добавлении новых типов задач (generate_thumbnail и т.п.).
- attempts — счётчик попыток для retry-логики. Пока не используется,
  но поле уже есть, чтобы потом не делать миграцию.
"""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import (
    Enum as SAEnum,
    Integer,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class TaskStatusEnum(str, enum.Enum):
    """
    Статус фоновой задачи. Терминальные — done/failed; восстановление
    при рестарте воркера определяется именно по этим статусам (см.
    миграция этапа 14, "production-ready").
    """

    pending = "pending"
    processing = "processing"
    done = "done"
    failed = "failed"


class Task(Base, TimestampMixin):
    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Тип задачи как строка, чтобы не делать миграцию при добавлении
    # новых обработчиков. Воркер диспатчит на хендлер по этому полю.
    type: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[TaskStatusEnum] = mapped_column(
        SAEnum(TaskStatusEnum, name="taskstatusenum"),
        default=TaskStatusEnum.pending,
        index=True,
    )
    # payload — что обрабатывать (show_id, entry_id, …). JSONB, потому
    # что схема меняется от типа к типу.
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    # result — что получилось. Обычно {"file_id": "<uuid>"} для PDF;
    # {"error": "..."} для failed. nullable, пока задача не закончена.
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Кто создал задачу. SET NULL — если юзер удалён, задача остаётся
    # как историческая запись.
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    # Количество попыток обработки. На этапе 8 не используется,
    # но позже worker сможет реализовать retry с backoff.
    attempts: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
