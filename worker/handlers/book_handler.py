import asyncio
import httpx

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
    print(f"Начинаю обработку книги: {payload['title']}")

    # Сообщить API: начал работу
    await update_task_status(task_id, "processing")

    # Имитация тяжёлой работы (5 секунд)
    await asyncio.sleep(5)

    # Результат работы
    result = {
        "book_id": payload["book_id"],
        "processed": True,
        "cover_url": f"/covers/{payload['book_id']}.jpg"
    }

    # Сообщить API: готово
    await update_task_status(task_id, "done", result=result)

    print(f"Книга обработана: {payload['title']}")
