from datetime import datetime

from app.exceptions import TaskNotFoundError
from app.schemas.task import TaskStatus, TaskStatusResponse


class InMemoryTaskStorage():
  def __init__(self):
    self._tasks: dict[str, TaskStatusResponse] = {}

  def create_task(self, task_id: str) -> TaskStatusResponse:
    task = TaskStatusResponse(
      task_id=task_id,
      status=TaskStatus.PENDING,
      created_at=datetime.utcnow(),
      updated_at=datetime.utcnow(),
    )
    self._tasks[task_id] = task
    return task
  
  def get_status(self, task_id: str) -> TaskStatusResponse | None:
    return self._tasks.get(task_id)
  
  def update_status(self, task_id:str, status: str, result=None, error=None) -> TaskStatusResponse:
    task = self._tasks.get(task_id)
    if not task:
      raise TaskNotFoundError(task_id)
    
    updated_task = task.model_copy(update={"status": status, "result": result, "error": error, "updated_at": datetime.utcnow()})
    self._tasks[task_id] = updated_task

    return updated_task

task_storage = InMemoryTaskStorage()