class TaskNotFoundError(Exception):
  def __init__(self, task_id: str):
    self.task_id = task_id
    super().__init__(f"Task with ID {task_id} not found.")

class RabbitMQConnectionError(Exception):
  def __init__(self, message: str):
    super().__init__(f"RabbitMQ connection error: {message}")