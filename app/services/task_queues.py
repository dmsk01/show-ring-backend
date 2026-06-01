"""
Единый источник соответствия `task.type` → имя RabbitMQ-очереди.

Вынесено отдельным модулем (без тяжёлых зависимостей вроде apscheduler),
чтобы:
- requeue_stuck_tasks (scheduler) и любые будущие потребители брали карту
  из одного места — раньше она дублировалась строковыми литералами и
  рассинхронилась: официальные документы и process_image выпадали из неё,
  и зависшая задача тихо умирала (review 2026-06-01);
- карту можно было покрыть unit-тестом без подъёма планировщика.

Все виды документов рендерит один document-воркер (очередь document_task);
обработка изображений — отдельный image-воркер (очередь image_task).
Имена очередей совпадают с константами в worker/main.py
(DOCUMENT_TASK_QUEUE / IMAGE_TASK_QUEUE) и routers/files.py.
"""

from __future__ import annotations

from app.schemas.task import DocumentKind

DOCUMENT_TASK_QUEUE = "document_task"
IMAGE_TASK_QUEUE = "image_task"
IMAGE_TASK_TYPE = "process_image"

# Карта строится из DocumentKind — новый вид документа автоматически
# попадает сюда, забыть его нельзя. image-задача добавляется явно.
QUEUE_FOR_TASK_TYPE: dict[str, str] = {
    kind.value: DOCUMENT_TASK_QUEUE for kind in DocumentKind
}
QUEUE_FOR_TASK_TYPE[IMAGE_TASK_TYPE] = IMAGE_TASK_QUEUE
