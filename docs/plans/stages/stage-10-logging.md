# Этап 10: Structured Logging на практике

### Цель

Внедрить структурированное логирование в реальный код. В Best Practices описан `JSONFormatter`, но в коде используется `print()`. Нужно исправить.

### Проблема с print()

```python
# ❌ Текущий подход в коде
print(f"Task {task_id} created")
print(f"Error: {e}")
```

Проблемы:
- Нет уровней (INFO/WARNING/ERROR)
- Нет timestamps
- Невозможно отфильтровать по полям
- В production не попадёт в систему мониторинга (ELK, Loki, CloudWatch)

### Целевой подход

```python
# ✅ Структурированное логирование
import logging

logger = logging.getLogger(__name__)

logger.info(
    "Task created",
    extra={"task_id": task_id, "action": "create_book", "queue": "book_tasks"}
)
```

### Файлы для изменения

#### 1. Создать app/logging_config.py

Файл `logging_config.py` уже описан в разделе Best Practices (JSONFormatter). Нужно создать его.

#### 2. Подключить в app/main.py

```python
# app/main.py
from app.logging_config import setup_logging
from app.config import get_settings

settings = get_settings()
setup_logging(json_format=settings.log_json)

logger = logging.getLogger(__name__)
```

#### 3. Добавить настройку в config.py

```python
# app/config.py
class Settings(BaseSettings):
    rabbitmq_url: str
    log_json: bool = False  # True для production, False для разработки
    log_level: str = "INFO"
```

#### 4. Заменить print() на logger во всех файлах

```python
# app/services/rabbit.py
logger = logging.getLogger(__name__)

# Было:  print(f"Connected to RabbitMQ")
# Стало:
logger.info("Connected to RabbitMQ", extra={"url": url})

# Было:  print(f"Published message to {queue_name}")
# Стало:
logger.info("Message published", extra={"queue": queue_name})

# worker/handlers/book_handler.py
logger = logging.getLogger(__name__)

# Было:  print(f"Processing book: {title}")
# Стало:
logger.info("Processing book", extra={"task_id": task_id, "title": title})
```

### Вывод в dev-режиме

```
2026-03-20 14:32:01 | INFO     | app.services.rabbit | Connected to RabbitMQ
2026-03-20 14:32:05 | INFO     | app.routers.books   | Task created
```

### Вывод в production (JSON)

```json
{"timestamp": "2026-03-20T14:32:01", "level": "INFO", "message": "Connected to RabbitMQ", "logger": "app.services.rabbit", "url": "amqp://guest:***@localhost/"}
{"timestamp": "2026-03-20T14:32:05", "level": "INFO", "message": "Task created", "logger": "app.routers.books", "task_id": "abc-123", "action": "create_book"}
```

### Что делать

1. Создать `app/logging_config.py` (код из раздела Best Practices)
2. Добавить `log_json` и `log_level` в `app/config.py`
3. Вызвать `setup_logging()` в `app/main.py`
4. Заменить все `print()` на `logger.info/warning/error` в:
   - `app/services/rabbit.py`
   - `app/routers/books.py`
   - `app/routers/tasks.py`
   - `worker/main.py`
   - `worker/handlers/book_handler.py`
5. Проверить вывод в обоих режимах
