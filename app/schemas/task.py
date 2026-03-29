from datetime import datetime
from enum import Enum
import uuid

from pydantic import BaseModel

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