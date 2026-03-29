# Этап 2: Практический сценарий с задачами

### Цель
Связать бизнес-логику (создание книги) с фоновой обработкой через очередь. Изучить паттерн **Task Queue** и **статус-машину задач**.

### Почему этот паттерн важен

**Проблема синхронного подхода:**
```python
# ❌ Плохо — клиент ждёт 30 секунд
@app.post("/books")
async def create_book(book: BookCreate):
    new_book = save_to_db(book)
    generate_cover(new_book)      # 10 сек
    index_for_search(new_book)    # 5 сек
    send_notification(new_book)   # 2 сек
    analyze_content(new_book)     # 15 сек
    return new_book  # Клиент ждал 32 секунды!
```

**Решение с очередью:**
```python
# ✅ Хорошо — клиент получает ответ за 50ms
@app.post("/books")
async def create_book(book: BookCreate):
    new_book = save_to_db(book)
    task_id = create_async_task(new_book)  # Отправить в очередь
    return {"book": new_book, "task_id": task_id}  # Мгновенный ответ

# Клиент может проверить статус: GET /tasks/{task_id}
```

### Архитектура Task Queue

```
┌──────────┐   POST /books   ┌──────────┐   publish    ┌──────────┐
│  Client  │ ──────────────> │  FastAPI │ ──────────── │ RabbitMQ │
└──────────┘                 └──────────┘              └────┬─────┘
     │                            │                         │
     │   {"id": 1, "task_id":    │                         │
     │    "abc-123"}             │                         │
     │ <────────────────────────  │                         │
     │                            │                         │ consume
     │                            │                         ▼
     │   GET /tasks/abc-123       │                   ┌──────────┐
     │ ─────────────────────────> │                   │  Worker  │
     │                            │                   └────┬─────┘
     │   {"status": "processing"} │                        │
     │ <───────────────────────── │                        │
     │                            │   PUT /tasks/abc-123   │
     │                            │ <───────────────────── │
     │   GET /tasks/abc-123       │                        │
     │ ─────────────────────────> │                        │
     │                            │                        │
     │   {"status": "done",       │                        │
     │    "result": {...}}        │                        │
     │ <───────────────────────── │                        │
```

### Машина состояний задачи

```
                    ┌─────────┐
                    │ PENDING │ ◄── Задача создана
                    └────┬────┘
                         │
            Worker взял в работу
                         │
                         ▼
                  ┌────────────┐
                  │ PROCESSING │
                  └──────┬─────┘
                         │
          ┌──────────────┴──────────────┐
          │                             │
    Успех │                             │ Ошибка
          │                             │
          ▼                             ▼
      ┌──────┐                     ┌────────┐
      │ DONE │                     │ FAILED │
      └──────┘                     └────────┘
```

**Почему статусы важны:**
- `PENDING` — задача в очереди, ждёт свободного воркера
- `PROCESSING` — воркер работает, показываем прогресс клиенту
- `DONE` — можно отдать результат
- `FAILED` — показать ошибку, возможно предложить retry

### Файлы для создания

#### `app/schemas/task.py`

```python
"""
Pydantic модели для задач.

Принципы:
1. Разделяем модели по назначению (Create, Response, Internal)
2. Используем Enum для ограниченных наборов значений
3. Добавляем валидацию где нужно
4. Документируем через docstrings и Field(description=...)
"""
from pydantic import BaseModel, Field
from datetime import datetime
from enum import Enum
import json

class TaskStatus(str, Enum):
    """
    Статусы задачи.

    str, Enum — чтобы сериализовалось как "pending", а не 0.
    """
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"

class TaskMessage(BaseModel):
    """
    Сообщение для RabbitMQ.

    Это внутренний формат — то, что путешествует через очередь.
    Клиент его не видит напрямую.
    """
    task_id: str = Field(..., description="UUID задачи")
    action: str = Field(..., description="Тип действия: process_book, send_email")
    payload: dict = Field(default_factory=dict, description="Данные для обработки")

    def to_json(self) -> str:
        """Сериализация для отправки в RabbitMQ"""
        return self.model_dump_json()

    @classmethod
    def from_json(cls, data: str) -> "TaskMessage":
        """Десериализация из RabbitMQ"""
        return cls.model_validate_json(data)

class TaskStatusResponse(BaseModel):
    """
    Ответ API о статусе задачи.

    Это публичный формат — то, что видит клиент.
    """
    task_id: str
    status: TaskStatus
    result: dict | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime

    # Для удобного обновления полей
    def with_status(
        self,
        status: TaskStatus,
        result: dict | None = None,
        error: str | None = None
    ) -> "TaskStatusResponse":
        """Создать копию с обновлённым статусом"""
        return self.model_copy(update={
            "status": status,
            "result": result,
            "error": error,
            "updated_at": datetime.utcnow()
        })

class StatusUpdateRequest(BaseModel):
    """Запрос на обновление статуса (от воркера)"""
    status: TaskStatus
    result: dict | None = None
    error: str | None = None
```

#### `app/services/task_storage.py`

```python
"""
Хранилище статусов задач.

В этом учебном проекте — in-memory dict.
В production это будет Redis или PostgreSQL.

Почему Redis лучше для статусов:
1. Быстрое чтение/запись (polling каждые 2 сек)
2. TTL — автоудаление старых задач
3. Pub/Sub — можно заменить polling на push

Почему PostgreSQL лучше для аудита:
1. Полная история всех задач
2. Сложные запросы (все failed за неделю)
3. Транзакции с бизнес-данными
"""
from datetime import datetime
from typing import Protocol
from app.schemas.task import TaskStatus, TaskStatusResponse
from app.exceptions import TaskNotFoundError
import logging

logger = logging.getLogger(__name__)

class TaskStorageProtocol(Protocol):
    """
    Интерфейс хранилища.

    Зачем Protocol?
    - Позволяет заменить реализацию (dict → Redis) без изменения кода
    - Упрощает тестирование (можно передать mock)
    - Документирует контракт
    """
    def create_task(self, task_id: str) -> TaskStatusResponse: ...
    def update_status(
        self,
        task_id: str,
        status: TaskStatus,
        result: dict | None = None,
        error: str | None = None
    ) -> TaskStatusResponse: ...
    def get_status(self, task_id: str) -> TaskStatusResponse | None: ...

class InMemoryTaskStorage:
    """
    In-memory реализация для разработки.

    Ограничения:
    - Данные теряются при рестарте
    - Не работает с несколькими инстансами API
    - Нет TTL (память течёт)
    """
    def __init__(self) -> None:
        self._tasks: dict[str, TaskStatusResponse] = {}

    def create_task(self, task_id: str) -> TaskStatusResponse:
        """Создать задачу со статусом PENDING"""
        now = datetime.utcnow()
        task = TaskStatusResponse(
            task_id=task_id,
            status=TaskStatus.PENDING,
            created_at=now,
            updated_at=now
        )
        self._tasks[task_id] = task
        logger.info(f"Task created: {task_id}")
        return task

    def update_status(
        self,
        task_id: str,
        status: TaskStatus,
        result: dict | None = None,
        error: str | None = None
    ) -> TaskStatusResponse:
        """
        Обновить статус задачи.

        Raises:
            TaskNotFoundError: если задача не существует
        """
        if task_id not in self._tasks:
            raise TaskNotFoundError(task_id)

        task = self._tasks[task_id]
        updated_task = task.with_status(status, result, error)
        self._tasks[task_id] = updated_task

        logger.info(
            f"Task {task_id} status updated: {task.status.value} -> {status.value}",
            extra={"task_id": task_id, "old_status": task.status.value, "new_status": status.value}
        )
        return updated_task

    def get_status(self, task_id: str) -> TaskStatusResponse | None:
        """Получить статус задачи"""
        return self._tasks.get(task_id)

# Глобальный экземпляр
# В production использовать Depends() для инъекции
task_storage = InMemoryTaskStorage()
```

#### `app/schemas/book.py`

```python
"""
Pydantic модели для книг.

Паттерн: отдельные модели для Create/Update/Response.
Это называется "DTO per operation" — каждая операция имеет свой контракт.
"""
from pydantic import BaseModel, Field

class BookBase(BaseModel):
    """Общие поля книги"""
    title: str = Field(..., min_length=1, max_length=200)
    author: str = Field(..., min_length=1, max_length=100)

class BookCreate(BookBase):
    """Данные для создания книги"""
    pass

class BookResponse(BookBase):
    """Книга в ответе API"""
    id: int

class BookWithTask(BaseModel):
    """Ответ при создании книги с фоновой задачей"""
    book: BookResponse
    task_id: str = Field(..., description="ID задачи для отслеживания статуса")
```

#### `app/routers/books.py`

```python
"""
CRUD эндпоинты для книг + интеграция с очередью задач.

Демонстрирует:
1. Отделение роутера от бизнес-логики
2. Использование Dependency Injection
3. Async/await для IO операций
4. Типизированные ответы
"""
from fastapi import APIRouter, HTTPException, Depends
from typing import Annotated
import uuid
import logging

from app.schemas.book import BookCreate, BookResponse, BookWithTask
from app.schemas.task import TaskMessage
from app.services.rabbit import RabbitMQService, rabbit_service
from app.services.task_storage import InMemoryTaskStorage, task_storage
from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/books", tags=["books"])

# Dependency injection types
RabbitDep = Annotated[RabbitMQService, Depends(lambda: rabbit_service)]
StorageDep = Annotated[InMemoryTaskStorage, Depends(lambda: task_storage)]

# In-memory storage (в production — база данных)
books: list[dict] = [
    {"id": 1, "title": "The Great Gatsby", "author": "F. Scott Fitzgerald"},
    {"id": 2, "title": "To Kill a Mockingbird", "author": "Harper Lee"},
    {"id": 3, "title": "1984", "author": "George Orwell"},
]

@router.get("", response_model=list[BookResponse])
async def get_books() -> list[dict]:
    """Получить список всех книг"""
    return books

@router.get("/{book_id}", response_model=BookResponse)
async def get_book(book_id: int) -> dict:
    """Получить книгу по ID"""
    for book in books:
        if book["id"] == book_id:
            return book
    raise HTTPException(status_code=404, detail="Book not found")

@router.post("", response_model=BookWithTask, status_code=201)
async def create_book(
    book: BookCreate,
    rabbit: RabbitDep,
    storage: StorageDep
) -> BookWithTask:
    """
    Создать книгу и запустить фоновую обработку.

    Что происходит:
    1. Книга сохраняется в "базу" (список)
    2. Создаётся задача со статусом PENDING
    3. Сообщение отправляется в RabbitMQ
    4. Клиент получает книгу + task_id для отслеживания

    Фоновая обработка может включать:
    - Генерация обложки через AI
    - Индексация для полнотекстового поиска
    - Отправка уведомления подписчикам
    - Анализ контента
    """
    # 1. Создать книгу
    new_id = max(b["id"] for b in books) + 1 if books else 1
    new_book = {"id": new_id, **book.model_dump()}
    books.append(new_book)

    # 2. Создать задачу
    task_id = str(uuid.uuid4())
    storage.create_task(task_id)

    # 3. Сформировать сообщение для очереди
    message = TaskMessage(
        task_id=task_id,
        action="process_book",
        payload={"book_id": new_id, "title": new_book["title"], "author": new_book["author"]}
    )

    # 4. Отправить в очередь
    await rabbit.publish(settings.queue_book_tasks, message.to_json())

    logger.info(
        f"Book created with background task",
        extra={"book_id": new_id, "task_id": task_id}
    )

    return BookWithTask(
        book=BookResponse(**new_book),
        task_id=task_id
    )

@router.delete("/{book_id}", status_code=204)
async def delete_book(book_id: int) -> None:
    """Удалить книгу по ID"""
    for i, book in enumerate(books):
        if book["id"] == book_id:
            books.pop(i)
            return
    raise HTTPException(status_code=404, detail="Book not found")
```

#### `app/routers/tasks.py` — расширенная версия

```python
"""
Эндпоинты для работы с задачами.

Два типа эндпоинтов:
1. Публичные — для клиентов (проверка статуса)
2. Внутренние — для воркеров (обновление статуса)
"""
from fastapi import APIRouter, HTTPException, Depends, Header
from typing import Annotated
import logging

from app.schemas.task import TaskStatusResponse, StatusUpdateRequest, TaskStatus
from app.services.rabbit import RabbitMQService, rabbit_service
from app.services.task_storage import InMemoryTaskStorage, task_storage
from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tasks", tags=["tasks"])

# Dependencies
RabbitDep = Annotated[RabbitMQService, Depends(lambda: rabbit_service)]
StorageDep = Annotated[InMemoryTaskStorage, Depends(lambda: task_storage)]

def verify_internal_key(x_api_key: str = Header(default="")) -> None:
    """
    Проверка ключа для внутренних эндпоинтов.

    Воркер должен передавать заголовок X-API-Key.
    Это простая защита от случайных вызовов.
    В production используйте JWT или mTLS.
    """
    if x_api_key != settings.internal_api_key:
        raise HTTPException(status_code=403, detail="Invalid API key")

# ============ Публичные эндпоинты ============

@router.get("/{task_id}", response_model=TaskStatusResponse)
async def get_task_status(task_id: str, storage: StorageDep) -> TaskStatusResponse:
    """
    Получить статус задачи.

    Клиент вызывает этот эндпоинт периодически (polling)
    чтобы узнать прогресс выполнения.

    Альтернатива polling — WebSocket (см. Этап 5).
    """
    task = storage.get_status(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    return task

# ============ Внутренние эндпоинты (для воркера) ============

@router.put(
    "/{task_id}/status",
    response_model=TaskStatusResponse,
    dependencies=[Depends(verify_internal_key)]
)
async def update_task_status(
    task_id: str,
    body: StatusUpdateRequest,
    storage: StorageDep
) -> TaskStatusResponse:
    """
    Обновить статус задачи (только для воркера).

    Требует заголовок X-API-Key.

    Воркер вызывает этот эндпоинт:
    - При начале обработки (PROCESSING)
    - При успешном завершении (DONE + result)
    - При ошибке (FAILED + error)
    """
    task = storage.get_status(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    updated = storage.update_status(
        task_id=task_id,
        status=body.status,
        result=body.result,
        error=body.error
    )

    return updated

# ============ Тестовый эндпоинт (из Этапа 1) ============

from pydantic import BaseModel

class TaskSendRequest(BaseModel):
    message: str

@router.post("/send")
async def send_task(body: TaskSendRequest, rabbit: RabbitDep) -> dict:
    """
    Отправить простое сообщение в очередь (для тестирования).

    Это остаётся для обратной совместимости с Этапом 1.
    """
    await rabbit.publish(settings.queue_tasks, body.message)
    return {"status": "sent", "message": body.message}
```

#### `worker/handlers/book_handler.py`

```python
"""
Обработчик задач типа "process_book".

Демонстрирует:
1. Асинхронную обработку
2. Обновление статуса через API
3. Обработку ошибок
4. Идемпотентность (можно безопасно повторить)
"""
import asyncio
import httpx
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Конфигурация (в production брать из переменных окружения)
API_URL = "http://localhost:8000"
INTERNAL_API_KEY = "change-me-in-production"

async def update_task_status(
    task_id: str,
    status: str,
    result: dict | None = None,
    error: str | None = None
) -> None:
    """
    Обновить статус задачи через API.

    Retry-логика важна: если API временно недоступен,
    не хотим терять результат работы.
    """
    payload: dict[str, Any] = {"status": status}
    if result:
        payload["result"] = result
    if error:
        payload["error"] = error

    async with httpx.AsyncClient() as client:
        for attempt in range(3):
            try:
                response = await client.put(
                    f"{API_URL}/tasks/{task_id}/status",
                    json=payload,
                    headers={"X-API-Key": INTERNAL_API_KEY},
                    timeout=10.0
                )
                response.raise_for_status()
                return
            except httpx.HTTPError as e:
                logger.warning(f"Failed to update status (attempt {attempt + 1}): {e}")
                if attempt < 2:
                    await asyncio.sleep(1 * (attempt + 1))  # Exponential backoff

        logger.error(f"Failed to update task {task_id} status after 3 attempts")

async def process_book(task_id: str, payload: dict) -> None:
    """
    Обработка книги.

    В реальном приложении здесь может быть:
    - Генерация обложки через Stable Diffusion / DALL-E
    - Извлечение ключевых слов для SEO
    - Конвертация в разные форматы (epub, mobi)
    - Отправка email автору
    - Индексация в Elasticsearch

    Принципы:
    1. Идемпотентность — повторный вызов с теми же данными безопасен
    2. Атомарность — либо всё сделано, либо ничего
    3. Observability — логируем всё важное
    """
    book_id = payload.get("book_id")
    title = payload.get("title", "Unknown")

    logger.info(f"Starting book processing", extra={
        "task_id": task_id,
        "book_id": book_id,
        "title": title
    })

    try:
        # 1. Сообщить что начали
        await update_task_status(task_id, "processing")

        # 2. Имитация работы
        # В реальности здесь будут вызовы внешних сервисов
        steps = [
            ("Analyzing content", 2),
            ("Generating cover", 3),
            ("Indexing for search", 1),
        ]

        for step_name, duration in steps:
            logger.info(f"Step: {step_name}", extra={"task_id": task_id})
            await asyncio.sleep(duration)

        # 3. Формируем результат
        result = {
            "book_id": book_id,
            "processed": True,
            "cover_url": f"/covers/{book_id}.jpg",
            "word_count": 45_000,
            "reading_time_minutes": 180,
            "keywords": ["fiction", "classic", "literature"]
        }

        # 4. Сообщить об успехе
        await update_task_status(task_id, "done", result=result)

        logger.info(f"Book processing completed", extra={
            "task_id": task_id,
            "book_id": book_id
        })

    except Exception as e:
        # 5. Обработка ошибок
        error_msg = str(e)
        logger.exception(f"Book processing failed", extra={
            "task_id": task_id,
            "book_id": book_id,
            "error": error_msg
        })

        await update_task_status(task_id, "failed", error=error_msg)

        # Re-raise чтобы сообщение вернулось в очередь (если настроено)
        raise
```

#### `worker/main.py`

```python
"""
Точка входа воркера.

Воркер — это отдельный процесс, который:
1. Подключается к RabbitMQ
2. Слушает очередь(и)
3. Обрабатывает сообщения
4. Подтверждает (ack) или отклоняет (nack)

Особенности:
- Graceful shutdown при SIGTERM
- Prefetch для контроля нагрузки
- Structured logging
"""
import asyncio
import signal
import json
import logging
import sys
from typing import Callable, Awaitable

import aio_pika
from aio_pika import IncomingMessage

# Добавляем корень проекта в path для импорта app.*
sys.path.insert(0, str(__file__).rsplit("worker", 1)[0])

from app.schemas.task import TaskMessage
from worker.handlers.book_handler import process_book

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
)
logger = logging.getLogger(__name__)

# Конфигурация
RABBITMQ_URL = "amqp://guest:guest@localhost/"
QUEUE_NAME = "book_tasks"
PREFETCH_COUNT = 1

# Реестр обработчиков
HANDLERS: dict[str, Callable[[str, dict], Awaitable[None]]] = {
    "process_book": process_book,
}

async def process_message(message: IncomingMessage) -> None:
    """
    Обработка входящего сообщения.

    async with message.process() гарантирует:
    - Если блок выполнился успешно → ack
    - Если исключение → nack, сообщение вернётся в очередь
    """
    async with message.process():
        body = message.body.decode()
        logger.info(f"Received message", extra={"body": body[:100]})

        try:
            task_msg = TaskMessage.from_json(body)
        except Exception as e:
            logger.error(f"Failed to parse message: {e}")
            return  # Не возвращаем в очередь — формат битый

        handler = HANDLERS.get(task_msg.action)
        if not handler:
            logger.warning(f"Unknown action: {task_msg.action}")
            return

        await handler(task_msg.task_id, task_msg.payload)

async def main() -> None:
    """Основной цикл воркера"""
    logger.info("Worker starting...")

    # Подключение к RabbitMQ
    connection = await aio_pika.connect_robust(RABBITMQ_URL)
    channel = await connection.channel()

    # Prefetch = сколько сообщений брать за раз
    # 1 = честное распределение между воркерами
    await channel.set_qos(prefetch_count=PREFETCH_COUNT)

    # Объявить очередь (должна совпадать с API)
    queue = await channel.declare_queue(QUEUE_NAME, durable=True)

    logger.info(f"Listening on queue: {QUEUE_NAME}")

    # Начать потребление
    await queue.consume(process_message)

    # Graceful shutdown
    stop_event = asyncio.Event()

    def signal_handler() -> None:
        logger.info("Shutdown signal received")
        stop_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError:
            # Windows не поддерживает add_signal_handler
            pass

    try:
        await stop_event.wait()
    except KeyboardInterrupt:
        pass
    finally:
        logger.info("Closing connection...")
        await connection.close()
        logger.info("Worker stopped")

if __name__ == "__main__":
    asyncio.run(main())
```

### Как проверить Этап 2

1. Запустить API: `uvicorn app.main:app --reload`
2. Запустить воркер: `python -m worker.main`
3. Создать книгу через Swagger UI: POST /books
4. Получить в ответе `task_id`
5. Проверять статус: GET /tasks/{task_id}
   - Сразу: `pending`
   - Через секунду: `processing`
   - Через ~6 секунд: `done` с результатом
