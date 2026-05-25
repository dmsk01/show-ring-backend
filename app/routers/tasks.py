import secrets

from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel

from app.config import settings
from app.schemas.task import StatusUpdateRequest, TaskStatusResponse
from app.services.task_storage import task_storage
from app.services.rabbit import rabbit_service


class TaskSendRequest(BaseModel):
    message: str


router = APIRouter(prefix="/tasks", tags=["tasks"])


def verify_internal_key(x_api_key: str = Header(default="")):
    # ИСПРАВЛЕНО: убран хардкод "secret-key" — ключ берётся из настроек.
    # Сравнение через secrets.compare_digest защищает от timing attack.
    expected = settings.internal_api_key
    if not expected or not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=403, detail="Invalid API key")


@router.post("/send", dependencies=[Depends(verify_internal_key)])
async def send_task(body: TaskSendRequest):
    # ИСПРАВЛЕНО: эндпоинт публикует в RabbitMQ — нельзя оставлять без
    # аутентификации, иначе любой внешний клиент может забить очередь.
    await rabbit_service.publish("tasks", body.message)
    return {"status": "Message sent to RabbitMQ", "message": body.message}


@router.get('/{task_id}')
def get_task_status(task_id: str) -> TaskStatusResponse:
    status = task_storage.get_status(task_id)
    if status is None:
        raise HTTPException(status_code=404, detail="Task not found")

    return status


@router.put('/{task_id}/status', dependencies=[Depends(verify_internal_key)])
def update_task_status(task_id: str, request: StatusUpdateRequest):
    response = task_storage.update_status(
        task_id, request.status, request.result, request.error)

    return response
