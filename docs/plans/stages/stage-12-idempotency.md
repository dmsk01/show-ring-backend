# Этап 12: Idempotency (идемпотентность обработки)

### Цель

Понять и реализовать идемпотентную обработку сообщений. Ключевой паттерн для надёжных распределённых систем.

### Проблема

Что если воркер:
1. Взял сообщение из очереди
2. Обработал его (создал книгу, обновил статус на "done")
3. Упал **до** отправки ACK в RabbitMQ

RabbitMQ не получил ACK -> считает сообщение необработанным -> отдаёт его другому воркеру. Книга создаётся **второй раз**.

```
Воркер:  получил -> обработал -> упал до ACK
RabbitMQ: нет ACK -> переотправить -> обработка второй раз!
```

### Решение: Idempotency Key

Каждое сообщение имеет уникальный `task_id`. Перед обработкой проверяем — не обработано ли оно уже.

```python
# worker/handlers/book_handler.py
import logging

logger = logging.getLogger(__name__)

class BookHandler:
    """
    Обработчик книг с идемпотентностью.
    Гарантия: повторная обработка того же task_id не создаст дубликат.
    """

    def __init__(self):
        # В production это Redis/БД, не in-memory
        self._processed: set[str] = set()

    async def handle(self, task_id: str, payload: dict) -> None:
        # 1. Проверка идемпотентности
        if task_id in self._processed:
            logger.warning(
                "Task already processed, skipping",
                extra={"task_id": task_id}
            )
            return

        # 2. Обработка
        logger.info("Processing book", extra={"task_id": task_id})
        await self._process_book(payload)

        # 3. Отметить как обработанное
        self._processed.add(task_id)
        logger.info("Task completed", extra={"task_id": task_id})

    async def _process_book(self, payload: dict) -> None:
        """Бизнес-логика обработки книги."""
        import asyncio
        await asyncio.sleep(5)
```

### Idempotency через Redis (production-подход)

```python
import redis.asyncio as redis

class IdempotencyStore:
    """
    Хранилище обработанных задач в Redis.
    TTL гарантирует что старые записи не копятся вечно.
    """

    def __init__(self, redis_client: redis.Redis, ttl: int = 86400):
        self.redis = redis_client
        self.ttl = ttl  # 24 часа по умолчанию

    async def is_processed(self, task_id: str) -> bool:
        """Проверить, обработана ли задача."""
        return await self.redis.exists(f"processed:{task_id}")

    async def mark_processed(self, task_id: str) -> None:
        """Отметить задачу как обработанную."""
        await self.redis.setex(f"processed:{task_id}", self.ttl, "1")
```

### Idempotency на стороне API

Клиенты тоже могут отправить дублирующий запрос (двойной клик, retry). Паттерн — `Idempotency-Key` в заголовке:

```python
# app/routers/books.py
from fastapi import Header

@router.post("/books")
async def create_book(
    book: BookCreate,
    rabbit: RabbitDep,
    storage: TaskStorageDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    if idempotency_key:
        existing = storage.get_by_idempotency_key(idempotency_key)
        if existing:
            return existing  # Возвращаем тот же результат

    task_id = storage.create_task(idempotency_key=idempotency_key)
    await rabbit.publish("book_tasks", ...)
    return BookWithTask(task_id=task_id, **book.model_dump())
```

### Когда идемпотентность критична

| Операция | Нужна? | Почему |
|----------|--------|--------|
| Создание книги | Да | Дубликаты в БД |
| Отправка email | Да | Пользователь получит 2 письма |
| Обновление статуса | Нет* | Повторное обновление безвредно |
| Удаление | Нет* | Повторное удаление — no-op |
| Списание денег | **Обязательно** | Двойное списание |

*\* — идемпотентны по природе (set-операции)*

### Что делать

1. Добавить проверку `task_id` в `book_handler.py` перед обработкой
2. Хранить обработанные ID в set (для обучения — in-memory, для production — Redis)
3. Добавить лог при пропуске дублирующего сообщения
4. Протестировать: отправить одно и то же сообщение дважды — обработать должно только один раз
