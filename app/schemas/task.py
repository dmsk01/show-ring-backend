from datetime import datetime
from enum import Enum
import uuid

from pydantic import BaseModel, ConfigDict

# ВНИМАНИЕ. В этом файле сосуществуют две группы схем:
# 1. Старые (TaskStatus / TaskMessage / TaskStatusResponse) — используются
#    in-memory storage'ом из этапа учебного примера. Не удалены ради
#    совместимости с роутером /tasks/* и worker/book_handler.
# 2. Новые DB-схемы для этапа 8 (DocumentKind, TaskResponse и т.п.) —
#    предназначены для реальной генерации документов через RabbitMQ
#    с сохранением статуса в PostgreSQL.


class TaskStatus(str, Enum):
    PENDING = 'pending'
    PROCESSING = 'processing'
    DONE = 'done'
    FAILED = 'failed'


class TaskMessage(BaseModel):
    task_id: uuid.UUID
    action: str
    payload: dict

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, data: str) -> "TaskMessage":
        return cls.model_validate_json(data)


class TaskStatusResponse(BaseModel):
    task_id: uuid.UUID
    status: TaskStatus
    result: dict | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class StatusUpdateRequest(BaseModel):
    status: TaskStatus
    result: dict | None = None
    error: str | None = None


# ---------------------------------------------------------------------
# Этап 8 — DB-backed задачи (новая модель app.models.task.Task)
# ---------------------------------------------------------------------


class DocumentKind(str, Enum):
    """
    Виды документов, которые умеет генерировать воркер. Названия идут
    как `type` в task'е — воркер диспатчит хендлер по этому полю.
    Хранятся как строки в БД, поэтому новый тип добавляется без миграции.
    """

    CATALOG = "generate_catalog"
    DIPLOMA = "generate_diploma"
    DIPLOMAS_BATCH = "generate_diplomas_batch"
    # Официальные документы РКФ (DOCX-шаблоны).
    CATALOG_OFFICIAL = "generate_catalog_official"
    DIPLOMA_OFFICIAL = "generate_diploma_official"
    DIPLOMAS_BATCH_OFFICIAL = "generate_diplomas_batch_official"
    RING_SHEETS_OFFICIAL = "generate_ring_sheets_official"
    CERTIFICATES_OFFICIAL = "generate_certificates_official"


class TaskResponse(BaseModel):
    """
    Ответ /tasks/{id} (DB-based). Унифицирован для всех типов задач:
    клиент опрашивает endpoint, пока status не станет done или failed,
    а потом читает result.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    type: str
    status: TaskStatus
    payload: dict
    # result структурирован под "что вернулось": для done — обычно
    # {"file_id": "...", "filename": "..."}; для failed — {"error": "..."}.
    result: dict | None = None
    attempts: int
    created_by: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime
