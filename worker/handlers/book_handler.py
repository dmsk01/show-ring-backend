"""
Учебный хендлер «обработка книги» (этап 1 учебного примера).
Production-хендлеры — document_handler.py / email_handler.py.
"""
import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)

API_URL = "http://localhost:8000"
API_KEY = "secret-key"


async def update_task_status(task_id, status, result=None, error=None):
    async with httpx.AsyncClient() as client:
        await client.put(
            f"{API_URL}/tasks/{task_id}/status",
            json={"status": status, "result": result, "error": error},
            headers={"X-API-Key": "secret-key"}
        )


async def process_book(task_id: str, payload: dict):
    # logger вместо print: в проде стандартный stdout-захват упаковывает
    # logger-сообщения в JSON; print остался бы plain-строкой без полей.
    logger.info("Начинаю обработку книги: %s", payload['title'])

    await update_task_status(task_id, "processing")
    await asyncio.sleep(5)  # имитация работы

    result = {
        "book_id": payload["book_id"],
        "processed": True,
        "cover_url": f"/covers/{payload['book_id']}.jpg"
    }

    await update_task_status(task_id, "done", result=result)
    logger.info("Книга обработана: %s", payload['title'])
