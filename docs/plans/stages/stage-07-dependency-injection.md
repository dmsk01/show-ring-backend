# Этап 7: Dependency Injection на практике

### Цель

Перевести роутеры с `app.state` на полноценный Dependency Injection через `Depends()`. Это фундамент для тестируемости и чистой архитектуры.

### Почему это важно

Сейчас в роутерах сервисы берутся через `request.app.state`:

```python
# ❌ Текущий подход — жёсткая связь с app.state
@router.post("/books")
async def create_book(request: Request, book: BookCreate):
    rabbit = request.app.state.rabbit_service
    storage = request.app.state.task_storage
```

Проблемы:
- **Тестирование** — нужно создавать полный FastAPI app, чтобы подменить зависимость
- **Неявность** — не видно, что нужно эндпоинту, пока не прочитаешь тело функции
- **Рефакторинг** — при смене архитектуры нужно менять каждый роутер

### Целевой подход

```python
# app/dependencies.py
from typing import Annotated
from fastapi import Depends, Request
from app.services.rabbit import RabbitMQService
from app.services.task_storage import InMemoryTaskStorage

def get_rabbit_service(request: Request) -> RabbitMQService:
    """Получить RabbitMQ сервис из app.state."""
    return request.app.state.rabbit_service

def get_task_storage(request: Request) -> InMemoryTaskStorage:
    """Получить хранилище задач из app.state."""
    return request.app.state.task_storage

# Тип-алиасы для читаемости
RabbitDep = Annotated[RabbitMQService, Depends(get_rabbit_service)]
TaskStorageDep = Annotated[InMemoryTaskStorage, Depends(get_task_storage)]
```

```python
# ✅ Целевой подход — явные зависимости
@router.post("/books")
async def create_book(
    book: BookCreate,
    rabbit: RabbitDep,        # Инжектится автоматически
    storage: TaskStorageDep,  # Инжектится автоматически
):
    task_id = storage.create_task()
    await rabbit.publish("book_tasks", TaskMessage(...).model_dump_json())
    return BookWithTask(task_id=task_id, **book.model_dump())
```

### Подмена зависимостей в тестах

```python
# tests/test_books.py
from app.dependencies import get_rabbit_service, get_task_storage

app.dependency_overrides[get_rabbit_service] = lambda: mock_rabbit
app.dependency_overrides[get_task_storage] = lambda: mock_storage
```

### Что делать

1. Создать `app/dependencies.py` с функциями-провайдерами
2. Определить тип-алиасы `RabbitDep`, `TaskStorageDep`
3. Переписать `app/routers/books.py` — заменить `request.app.state` на `Depends()`
4. Переписать `app/routers/tasks.py` — аналогично
5. Проверить, что всё работает как раньше
