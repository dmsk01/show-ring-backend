# Best Practices: Python & FastAPI

### Почему эти практики важны

В production-коде качество архитектуры определяет:
- **Maintainability** — насколько легко вносить изменения через 6 месяцев
- **Testability** — можно ли покрыть код тестами без боли
- **Scalability** — выдержит ли система рост нагрузки
- **Debuggability** — как быстро найти причину бага в 3 часа ночи

### 1. Типизация (Type Hints)

**Зачем:** IDE автодополнение, раннее обнаружение ошибок, самодокументирующийся код.

```python
# ❌ Плохо — непонятно что принимает и возвращает
def process_book(data):
    return {"id": data["id"], "processed": True}

# ✅ Хорошо — контракт очевиден
from typing import TypedDict

class BookPayload(TypedDict):
    id: int
    title: str

class ProcessResult(TypedDict):
    id: int
    processed: bool

def process_book(data: BookPayload) -> ProcessResult:
    return {"id": data["id"], "processed": True}
```

**Инструменты:**
- `mypy` — статический анализатор типов
- `pyright` — быстрая альтернатива от Microsoft
- `ruff` — линтер + форматтер (замена flake8, black, isort)

### 2. Pydantic для валидации

**Зачем:** Автоматическая валидация на границах системы, сериализация, документация OpenAPI.

```python
from pydantic import BaseModel, Field, field_validator
from datetime import datetime
from enum import Enum

class TaskStatus(str, Enum):
    """
    Почему str, Enum?
    - str: сериализуется в JSON как строка, а не число
    - Enum: ограниченный набор значений, IDE подсказки
    """
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"

class TaskCreate(BaseModel):
    """
    Модель для создания задачи (входящие данные от клиента).

    Отделяем от TaskResponse — клиент не должен задавать id, created_at.
    Это паттерн "Command/Query Separation" на уровне моделей.
    """
    action: str = Field(
        ...,  # ... означает обязательное поле
        min_length=1,
        max_length=100,
        description="Тип действия: process_book, send_email"
    )
    payload: dict = Field(
        default_factory=dict,
        description="Данные для обработки"
    )

    @field_validator("action")
    @classmethod
    def validate_action(cls, v: str) -> str:
        """Кастомная валидация — разрешаем только известные действия"""
        allowed = {"process_book", "send_email", "generate_report"}
        if v not in allowed:
            raise ValueError(f"Unknown action. Allowed: {allowed}")
        return v

class TaskResponse(BaseModel):
    """Модель для ответа API (исходящие данные)"""
    task_id: str
    status: TaskStatus
    result: dict | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "task_id": "550e8400-e29b-41d4-a716-446655440000",
                    "status": "done",
                    "result": {"processed": True},
                    "error": None,
                    "created_at": "2024-01-15T10:30:00Z",
                    "updated_at": "2024-01-15T10:30:05Z"
                }
            ]
        }
    }
```

### 3. Dependency Injection в FastAPI

**Зачем:**
- **Тестируемость** — легко подменить зависимость на mock
- **Переиспользование** — общая логика (auth, db session) в одном месте
- **Явные зависимости** — видно что нужно эндпоинту

```python
# app/dependencies.py
from typing import Annotated
from fastapi import Depends, HTTPException, Header
from app.services.rabbit import RabbitMQService, rabbit_service
from app.services.task_storage import TaskStorage, task_storage

# Тип-алиас для читаемости
RabbitDep = Annotated[RabbitMQService, Depends(lambda: rabbit_service)]
TaskStorageDep = Annotated[TaskStorage, Depends(lambda: task_storage)]

async def get_api_key(x_api_key: str = Header(default=None)) -> str | None:
    """
    Извлечение API ключа из заголовка.
    Для внутренних эндпоинтов (worker -> API).
    """
    return x_api_key

def require_internal_api_key(
    api_key: Annotated[str | None, Depends(get_api_key)]
) -> str:
    """
    Защита внутренних эндпоинтов.
    Worker должен передавать секретный ключ.
    """
    # В production это должно быть в переменных окружения
    if api_key != "internal-secret-key":
        raise HTTPException(status_code=403, detail="Invalid API key")
    return api_key

# Использование в роутере:
# @router.put("/tasks/{task_id}/status")
# async def update_status(
#     task_id: str,
#     body: StatusUpdate,
#     storage: TaskStorageDep,  # Инжектится автоматически
#     _: Annotated[str, Depends(require_internal_api_key)]  # Проверка ключа
# ):
#     storage.update_status(task_id, body.status)
```

### 4. Структурированное логирование

**Зачем:** В production логи — единственный способ понять что происходит. JSON-формат позволяет индексировать и искать в ELK/Loki.

```python
# app/logging_config.py
import logging
import sys
from typing import Any
import json
from datetime import datetime

class JSONFormatter(logging.Formatter):
    """
    Форматтер для структурированных логов.
    Каждая строка — валидный JSON, удобно парсить.
    """
    def format(self, record: logging.LogRecord) -> str:
        log_data: dict[str, Any] = {
            "timestamp": datetime.utcnow().isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
        }

        # Добавляем extra-поля если есть
        if hasattr(record, "task_id"):
            log_data["task_id"] = record.task_id
        if hasattr(record, "action"):
            log_data["action"] = record.action
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_data)

def setup_logging(json_format: bool = False) -> None:
    """
    Настройка логирования.
    json_format=True для production, False для локальной разработки.
    """
    handler = logging.StreamHandler(sys.stdout)

    if json_format:
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
        ))

    # Настраиваем корневой логгер
    logging.root.handlers = [handler]
    logging.root.setLevel(logging.INFO)

    # Уменьшаем шум от библиотек
    logging.getLogger("aio_pika").setLevel(logging.WARNING)
    logging.getLogger("aiormq").setLevel(logging.WARNING)

# Использование в коде:
# logger = logging.getLogger(__name__)
# logger.info("Processing task", extra={"task_id": task_id, "action": action})
```

### 5. Обработка ошибок

**Зачем:** Единообразные ответы об ошибках, централизованная обработка, полезные сообщения для клиента.

```python
# app/exceptions.py
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

class ErrorResponse(BaseModel):
    """Стандартный формат ответа об ошибке"""
    error: str
    detail: str | None = None
    code: str  # Машиночитаемый код для фронтенда

class TaskNotFoundError(Exception):
    """Задача не найдена"""
    def __init__(self, task_id: str):
        self.task_id = task_id
        super().__init__(f"Task {task_id} not found")

class RabbitMQConnectionError(Exception):
    """Ошибка подключения к RabbitMQ"""
    pass

class TaskProcessingError(Exception):
    """Ошибка при обработке задачи"""
    def __init__(self, task_id: str, reason: str):
        self.task_id = task_id
        self.reason = reason
        super().__init__(f"Task {task_id} processing failed: {reason}")

# app/main.py — регистрация обработчиков
#
# @app.exception_handler(TaskNotFoundError)
# async def task_not_found_handler(request: Request, exc: TaskNotFoundError):
#     return JSONResponse(
#         status_code=404,
#         content=ErrorResponse(
#             error="Task not found",
#             detail=f"Task with id '{exc.task_id}' does not exist",
#             code="TASK_NOT_FOUND"
#         ).model_dump()
#     )
#
# @app.exception_handler(RabbitMQConnectionError)
# async def rabbitmq_error_handler(request: Request, exc: RabbitMQConnectionError):
#     return JSONResponse(
#         status_code=503,
#         content=ErrorResponse(
#             error="Service temporarily unavailable",
#             detail="Message broker is not available",
#             code="BROKER_UNAVAILABLE"
#         ).model_dump()
#     )
```

### 6. Конфигурация через Pydantic Settings

**Зачем:** Валидация конфига при старте, типизация, документация переменных окружения.

```python
# app/config.py
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, field_validator
from functools import lru_cache

class Settings(BaseSettings):
    """
    Конфигурация приложения.

    Все значения читаются из переменных окружения или .env файла.
    Валидация происходит при создании экземпляра — если что-то не так,
    приложение не запустится (fail fast).
    """
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,  # RABBITMQ_URL = rabbitmq_url
    )

    # RabbitMQ
    rabbitmq_url: str = Field(
        default="amqp://guest:guest@localhost/",
        description="AMQP connection string"
    )
    rabbitmq_prefetch_count: int = Field(
        default=1,
        ge=1,
        le=100,
        description="Сколько сообщений воркер берёт за раз"
    )

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    debug: bool = False

    # Внутренняя коммуникация
    internal_api_key: str = Field(
        default="change-me-in-production",
        min_length=16,
        description="Ключ для worker -> API коммуникации"
    )

    # Очереди (имена задаются здесь, а не хардкодятся)
    queue_tasks: str = "tasks"
    queue_book_tasks: str = "book_tasks"
    exchange_events: str = "events"
    exchange_app_events: str = "app_events"

    @field_validator("rabbitmq_url")
    @classmethod
    def validate_rabbitmq_url(cls, v: str) -> str:
        if not v.startswith(("amqp://", "amqps://")):
            raise ValueError("RabbitMQ URL must start with amqp:// or amqps://")
        return v

@lru_cache
def get_settings() -> Settings:
    """
    Кэшированное создание настроек.
    lru_cache гарантирует что Settings() вызывается один раз.
    """
    return Settings()

# Для удобства импорта
settings = get_settings()
```
