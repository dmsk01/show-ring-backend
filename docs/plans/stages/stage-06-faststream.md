# Этап 6: FastStream

### Цель
Изучить современный декларативный подход к работе с брокерами сообщений через FastStream — фреймворк, который предоставляет FastAPI-подобный синтаксис для consumer'ов.

### Почему FastStream

**Проблемы с aio-pika напрямую:**
```python
# ❌ Много boilerplate кода
async def process_message(message: IncomingMessage):
    async with message.process():
        body = message.body.decode()
        data = json.loads(body)  # Ручной парсинг
        # Нет валидации
        # Нет типизации
        await handle_book(data)

queue = await channel.declare_queue("books")
await queue.consume(process_message)
```

**FastStream решает:**
```python
# ✅ Декларативно, типизировано, валидация из коробки
from faststream.rabbit import RabbitBroker

broker = RabbitBroker("amqp://guest:guest@localhost/")

@broker.subscriber("book_tasks")
async def process_book(task: TaskMessage) -> None:
    """Pydantic модель автоматически валидируется"""
    await handle_book(task.task_id, task.payload)
```

### Преимущества FastStream

| Функция | aio-pika | FastStream |
|---------|----------|------------|
| **Валидация** | Ручная | Pydantic из коробки |
| **Типизация** | Частичная | Полная |
| **Тестирование** | Нужен RabbitMQ | TestBroker (in-memory) |
| **Документация** | Нет | AsyncAPI автогенерация |
| **Retry/DLQ** | Ручная настройка | Middleware |
| **Интеграция с FastAPI** | Ручная | Встроенная |
| **Поддержка брокеров** | Только RabbitMQ | RabbitMQ, Kafka, Redis, NATS |

### Архитектура с FastStream

```
┌─────────────┐     HTTP      ┌──────────────────────────┐
│   Vue.js    │ ────────────> │  FastAPI + FastStream    │
│  Frontend   │ <──────────── │  (один процесс!)         │
└─────────────┘               └────────────┬─────────────┘
                                          │
                                          │ AMQP
                                          ▼
                                   ┌─────────────┐
                                   │  RabbitMQ   │
                                   │   Broker    │
                                   └─────────────┘
```

**Ключевое отличие:** FastStream позволяет запускать consumer'ов в том же процессе, что и FastAPI, или отдельно.

### Файлы для создания

#### `requirements.txt` — добавить

```
faststream[rabbit]>=0.5.0
```

#### `app/broker.py`

```python
"""
Настройка FastStream брокера.

FastStream предоставляет декларативный способ работы с очередями,
аналогичный тому, как FastAPI работает с HTTP.
"""
from faststream.rabbit import RabbitBroker
from app.config import settings

# Создаём брокер (аналог FastAPI app)
broker = RabbitBroker(settings.rabbitmq_url)
```

#### `app/consumers/book_consumer.py`

```python
"""
Consumer для обработки книг через FastStream.

Декларативный стиль: декоратор @broker.subscriber определяет
какую очередь слушать и какой тип сообщений ожидать.
"""
from faststream.rabbit import RabbitRouter
from pydantic import BaseModel
import asyncio
import logging

from app.schemas.task import TaskMessage, TaskStatus
from app.services.task_storage import task_storage

logger = logging.getLogger(__name__)

# Router группирует связанные handlers (как APIRouter в FastAPI)
router = RabbitRouter()

class BookProcessResult(BaseModel):
    """Результат обработки книги"""
    book_id: int
    processed: bool
    cover_url: str
    word_count: int

@router.subscriber("book_tasks")
async def process_book(message: TaskMessage) -> None:
    """
    Обработчик задач обработки книг.

    Преимущества FastStream:
    1. TaskMessage автоматически десериализуется из JSON
    2. Валидация Pydantic из коробки
    3. Автоматический ack при успехе, nack при исключении
    4. Типизация для IDE
    """
    task_id = str(message.task_id)
    payload = message.payload

    logger.info(f"Processing book: {payload.get('title')}", extra={
        "task_id": task_id,
        "book_id": payload.get("book_id")
    })

    try:
        # Обновляем статус на PROCESSING
        task_storage.update_status(task_id, TaskStatus.PROCESSING)

        # Имитация обработки
        await asyncio.sleep(3)

        # Результат
        result = BookProcessResult(
            book_id=payload["book_id"],
            processed=True,
            cover_url=f"/covers/{payload['book_id']}.jpg",
            word_count=45000
        )

        # Обновляем статус на DONE
        task_storage.update_status(
            task_id,
            TaskStatus.DONE,
            result=result.model_dump()
        )

        logger.info(f"Book processed successfully", extra={"task_id": task_id})

    except Exception as e:
        logger.exception(f"Book processing failed", extra={"task_id": task_id})
        task_storage.update_status(
            task_id,
            TaskStatus.FAILED,
            error=str(e)
        )
        raise  # FastStream вернёт в очередь

@router.subscriber("email_tasks")
async def send_email(
    to: str,
    subject: str,
    body: str
) -> dict:
    """
    Пример: простые параметры вместо Pydantic модели.

    FastStream поддерживает оба варианта.
    """
    logger.info(f"Sending email to {to}: {subject}")
    await asyncio.sleep(1)
    return {"sent": True, "to": to}
```

#### `app/consumers/__init__.py`

```python
from app.consumers.book_consumer import router as book_router

# Экспортируем все роутеры
__all__ = ["book_router"]
```

#### `app/main.py` — интеграция FastStream с FastAPI

```python
"""
Интеграция FastAPI + FastStream.

Два варианта:
1. Lifespan — consumers работают в том же процессе
2. Отдельный запуск — python -m app.worker
"""
from fastapi import FastAPI
from contextlib import asynccontextmanager
import logging

from app.config import settings
from app.broker import broker
from app.consumers import book_router
from app.routers import books, tasks

logger = logging.getLogger(__name__)

# Подключаем роутеры FastStream к брокеру
broker.include_router(book_router)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Lifespan управляет жизненным циклом брокера.

    При старте:
    - Подключается к RabbitMQ
    - Запускает все consumers в фоне

    При остановке:
    - Gracefully завершает обработку
    - Закрывает соединения
    """
    logger.info("Starting FastStream broker...")

    # Запуск брокера и всех consumers
    await broker.start()
    logger.info("FastStream broker started")

    yield

    # Остановка
    logger.info("Stopping FastStream broker...")
    await broker.close()
    logger.info("FastStream broker stopped")

app = FastAPI(
    title="Books API with FastStream",
    lifespan=lifespan
)

# HTTP роутеры
app.include_router(tasks.router)
app.include_router(books.router)
```

#### `app/services/publisher.py`

```python
"""
Publisher для отправки сообщений через FastStream.

Преимущество: типизированные publisher'ы с валидацией.
"""
from faststream.rabbit import RabbitBroker
from app.broker import broker
from app.schemas.task import TaskMessage
import logging

logger = logging.getLogger(__name__)

# Типизированный publisher
book_tasks_publisher = broker.publisher("book_tasks")

async def publish_book_task(message: TaskMessage) -> None:
    """
    Отправить задачу на обработку книги.

    FastStream автоматически сериализует Pydantic модель в JSON.
    """
    await book_tasks_publisher.publish(message)
    logger.info(f"Published book task", extra={
        "task_id": str(message.task_id),
        "action": message.action
    })

# Альтернативный способ — прямая публикация
async def publish_to_queue(queue_name: str, message: dict) -> None:
    """Публикация произвольного сообщения"""
    await broker.publish(message, queue=queue_name)
```

#### `app/routers/books.py` — использование FastStream publisher

```python
"""
Роутер книг с FastStream publisher.
"""
import uuid
from fastapi import APIRouter, HTTPException

from app.schemas.book import BookCreate, BookResponse, BookWithTask
from app.schemas.task import TaskMessage
from app.services.task_storage import task_storage
from app.services.publisher import publish_book_task

router = APIRouter(prefix="/books", tags=["books"])

books: list[dict] = [
    {"id": 1, "title": "The Great Gatsby", "author": "F. Scott Fitzgerald"},
    {"id": 2, "title": "1984", "author": "George Orwell"},
]

@router.post("/", response_model=BookWithTask, status_code=201)
async def create_book(body: BookCreate) -> BookWithTask:
    """
    Создать книгу и отправить на обработку.

    Использует FastStream publisher вместо прямого aio-pika.
    """
    new_id = max(b["id"] for b in books) + 1 if books else 1
    new_book = {"id": new_id, "title": body.title, "author": body.author}
    books.append(new_book)

    # Создать задачу
    task_id = str(uuid.uuid4())
    task_storage.create_task(task_id)

    # Отправить через FastStream (типизировано!)
    message = TaskMessage(
        task_id=task_id,
        action="process_book",
        payload={"book_id": new_id, "title": body.title, "author": body.author}
    )
    await publish_book_task(message)

    return BookWithTask(
        book=BookResponse(**new_book),
        task_id=task_id
    )
```

### RPC паттерн (Request-Reply)

FastStream поддерживает RPC — синхронный запрос через очередь:

```python
# app/consumers/rpc_consumer.py
from faststream.rabbit import RabbitRouter

router = RabbitRouter()

@router.subscriber("rpc_queue")
async def calculate(x: int, y: int) -> int:
    """
    RPC handler — возвращает результат вызывающему.

    Клиент отправляет запрос и ждёт ответа.
    """
    return x + y

# app/routers/rpc.py
from fastapi import APIRouter
from app.broker import broker

router = APIRouter(prefix="/rpc", tags=["rpc"])

@router.get("/calculate")
async def calculate_via_rpc(x: int, y: int) -> dict:
    """
    Вызвать RPC через RabbitMQ.

    Полезно когда вычисление должен делать специализированный worker.
    """
    result = await broker.publish(
        {"x": x, "y": y},
        queue="rpc_queue",
        rpc=True,  # Ждать ответ
        rpc_timeout=10.0
    )
    return {"result": result}
```

### Тестирование с TestRabbitBroker

```python
# tests/test_book_consumer.py
"""
Тестирование без реального RabbitMQ!

FastStream предоставляет in-memory TestBroker.
"""
import pytest
from faststream.rabbit import TestRabbitBroker

from app.broker import broker
from app.consumers.book_consumer import process_book
from app.schemas.task import TaskMessage

@pytest.fixture
def test_broker():
    """Создать тестовый брокер"""
    return TestRabbitBroker(broker)

@pytest.mark.asyncio
async def test_process_book(test_broker):
    """Тест обработки книги без RabbitMQ"""

    async with test_broker:
        # Подготовка
        message = TaskMessage(
            task_id="test-123",
            action="process_book",
            payload={"book_id": 1, "title": "Test Book", "author": "Test"}
        )

        # Действие — отправляем сообщение
        await broker.publish(message, queue="book_tasks")

        # Проверка — handler был вызван
        process_book.mock.assert_called_once()

@pytest.mark.asyncio
async def test_rpc_calculate(test_broker):
    """Тест RPC вызова"""

    async with test_broker:
        result = await broker.publish(
            {"x": 2, "y": 3},
            queue="rpc_queue",
            rpc=True
        )
        assert result == 5
```

### Middleware для retry и логирования

```python
# app/middleware.py
from faststream import BaseMiddleware
from faststream.rabbit import RabbitMessage
import logging

logger = logging.getLogger(__name__)

class LoggingMiddleware(BaseMiddleware):
    """Логирование всех входящих сообщений"""

    async def on_receive(self) -> None:
        logger.info(f"Received message", extra={
            "queue": self.context.get("queue"),
            "message_id": self.msg.message_id
        })

    async def after_processed(self, exc_type=None, exc_val=None, exc_tb=None):
        if exc_type:
            logger.error(f"Message processing failed: {exc_val}")
        else:
            logger.info("Message processed successfully")

class RetryMiddleware(BaseMiddleware):
    """
    Автоматический retry с экспоненциальной задержкой.

    В production обычно используют DLQ вместо retry.
    """
    max_retries: int = 3

    async def after_processed(self, exc_type=None, exc_val=None, exc_tb=None):
        if exc_type and hasattr(self.msg, "headers"):
            retry_count = self.msg.headers.get("x-retry-count", 0)
            if retry_count < self.max_retries:
                # Переотправить с увеличенным счётчиком
                await self.broker.publish(
                    self.msg.body,
                    queue=self.context.get("queue"),
                    headers={"x-retry-count": retry_count + 1}
                )

# Подключение middleware
# broker.add_middleware(LoggingMiddleware)
# broker.add_middleware(RetryMiddleware)
```

### Отдельный запуск worker

```python
# app/worker.py
"""
Отдельный запуск FastStream worker.

Использовать когда:
- Worker должен масштабироваться отдельно от API
- Нужно запускать на других серверах
- Требуется изоляция процессов
"""
import asyncio
import logging

from app.broker import broker
from app.consumers import book_router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Подключаем роутеры
broker.include_router(book_router)

async def main():
    logger.info("Starting FastStream worker...")
    await broker.start()
    logger.info("Worker started. Waiting for messages...")

    try:
        await asyncio.Future()  # Run forever
    except KeyboardInterrupt:
        pass
    finally:
        await broker.close()
        logger.info("Worker stopped")

if __name__ == "__main__":
    asyncio.run(main())
```

### CLI запуск через faststream

```bash
# Встроенный CLI (аналог uvicorn)
faststream run app.worker:broker --reload

# Или с переменными окружения
RABBITMQ_URL=amqp://user:pass@rabbit:5672/ faststream run app.worker:broker
```

### Как проверить Этап 6

1. Установить FastStream:
   ```bash
   pip install "faststream[rabbit]"
   ```

2. Запустить приложение (consumers в том же процессе):
   ```bash
   uvicorn app.main:app --reload
   ```

3. Создать книгу: POST /books
   - Сообщение отправляется через FastStream publisher
   - Consumer в том же процессе обрабатывает

4. Проверить статус: GET /tasks/{task_id}
   - pending → processing → done

5. Запустить тесты:
   ```bash
   pytest tests/test_book_consumer.py -v
   ```

6. (Опционально) Отдельный worker:
   ```bash
   # Терминал 1: только API
   FASTSTREAM_DISABLE_CONSUMERS=1 uvicorn app.main:app

   # Терминал 2: только worker
   python -m app.worker
   ```

### Сравнение: aio-pika vs FastStream

| Аспект | aio-pika (Этапы 1-2) | FastStream (Этап 6) |
|--------|----------------------|---------------------|
| **Код consumer** | ~30 строк | ~10 строк |
| **Валидация** | Ручная | Автоматическая |
| **Тесты** | Нужен RabbitMQ | TestBroker |
| **Документация** | Swagger только для HTTP | AsyncAPI для очередей |
| **Гибкость** | Максимальная | Высокая |
| **Когда использовать** | Сложные кастомные сценарии | 90% задач |
