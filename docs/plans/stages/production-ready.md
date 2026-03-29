# Production-Ready практики

### 1. Health Checks

```python
# app/routers/health.py
from fastapi import APIRouter
from app.services.rabbit import rabbit_service

router = APIRouter(tags=["health"])

@router.get("/health")
async def health_check() -> dict:
    """
    Health check для Kubernetes/Docker.

    Проверяет:
    - Приложение запущено
    - RabbitMQ доступен
    """
    rabbit_ok = rabbit_service.connection is not None and not rabbit_service.connection.is_closed

    return {
        "status": "healthy" if rabbit_ok else "degraded",
        "checks": {
            "rabbitmq": "ok" if rabbit_ok else "unavailable"
        }
    }

@router.get("/ready")
async def readiness_check() -> dict:
    """
    Readiness probe — готов ли сервис принимать трафик.
    """
    return {"status": "ready"}
```

### 2. Graceful Shutdown

```python
# app/main.py
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Starting up...")
    await rabbit_service.connect(settings.rabbitmq_url)

    # Готовы обрабатывать запросы
    yield

    # Shutdown
    logger.info("Shutting down...")

    # 1. Перестать принимать новые задачи
    # 2. Дождаться завершения текущих
    # 3. Закрыть соединения

    await rabbit_service.close()
    logger.info("Shutdown complete")
```

### 3. Retry с экспоненциальной задержкой

```python
import asyncio
from typing import TypeVar, Callable, Awaitable

T = TypeVar("T")

async def retry_with_backoff(
    func: Callable[[], Awaitable[T]],
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0
) -> T:
    """
    Повторить операцию с экспоненциальной задержкой.

    Паттерн для надёжной работы с внешними сервисами.
    """
    last_exception = None

    for attempt in range(max_attempts):
        try:
            return await func()
        except Exception as e:
            last_exception = e

            if attempt < max_attempts - 1:
                delay = min(base_delay * (2 ** attempt), max_delay)
                logger.warning(
                    f"Attempt {attempt + 1} failed, retrying in {delay}s: {e}"
                )
                await asyncio.sleep(delay)

    raise last_exception
```

### 4. Dead Letter Queue (DLQ)

```python
# Очередь для "мёртвых" сообщений — тех, которые не удалось обработать

# При объявлении основной очереди:
queue = await channel.declare_queue(
    "tasks",
    durable=True,
    arguments={
        "x-dead-letter-exchange": "",
        "x-dead-letter-routing-key": "tasks_dlq",
        "x-message-ttl": 86400000,  # 24 часа
    }
)

# Отдельно объявить DLQ:
dlq = await channel.declare_queue("tasks_dlq", durable=True)
```

### 5. Метрики (Prometheus)

```python
# pip install prometheus-client

from prometheus_client import Counter, Histogram, Gauge

# Счётчики
tasks_total = Counter(
    "tasks_total",
    "Total tasks processed",
    ["action", "status"]
)

# Гистограммы
task_duration = Histogram(
    "task_duration_seconds",
    "Task processing duration",
    ["action"]
)

# Gauges
tasks_in_progress = Gauge(
    "tasks_in_progress",
    "Currently processing tasks"
)

# Использование:
tasks_in_progress.inc()
with task_duration.labels(action="process_book").time():
    await process_book(task_id, payload)
tasks_in_progress.dec()
tasks_total.labels(action="process_book", status="success").inc()
```
