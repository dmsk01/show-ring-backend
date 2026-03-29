from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel
from app.schemas.task import StatusUpdateRequest, TaskStatusResponse
from app.services.task_storage import task_storage
from app.services.rabbit import rabbit_service

class TaskSendRequest(BaseModel):
    message: str

router = APIRouter(prefix="/tasks", tags=["tasks"])

def verify_internal_key(x_api_key: str = Header(default="")):
    if x_api_key != "secret-key":  # Потом возьмём из config
        raise HTTPException(status_code=403, detail="Invalid API key")

@router.post("/send")
async def send_task(body: TaskSendRequest):
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
    response = task_storage.update_status(task_id, request.status, request.result, request.error)

    return response