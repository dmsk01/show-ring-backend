# Этап 9: Тестирование (pytest + httpx)

### Цель

Научиться тестировать FastAPI-приложение: эндпоинты, сервисы, интеграцию с RabbitMQ.

### Почему тестирование критично

- **Рефакторинг без страха** — тесты поймают регрессии
- **Документация** — тест показывает как API должен работать
- **CI/CD** — без тестов нет автоматического деплоя
- **Уверенность** — знаешь, что изменение не сломало другие части

### Инструменты

```
pip install pytest pytest-asyncio httpx
```

| Инструмент | Назначение |
|-----------|-----------|
| `pytest` | Фреймворк для тестирования |
| `pytest-asyncio` | Поддержка async/await в тестах |
| `httpx.AsyncClient` | HTTP-клиент для тестирования FastAPI |

### Структура тестов

```
tests/
├── __init__.py
├── conftest.py            # Общие фикстуры
├── test_books.py          # Тесты эндпоинтов книг
├── test_tasks.py          # Тесты эндпоинтов задач
└── test_task_storage.py   # Unit-тесты хранилища
```

### Конфигурация pytest

```ini
# pytest.ini
[pytest]
asyncio_mode = auto
testpaths = tests
```

### Фикстуры (conftest.py)

```python
# tests/conftest.py
import pytest
from unittest.mock import AsyncMock
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.dependencies import get_rabbit_service, get_task_storage
from app.services.task_storage import InMemoryTaskStorage

@pytest.fixture
def task_storage():
    """Свежее хранилище для каждого теста."""
    return InMemoryTaskStorage()

@pytest.fixture
def mock_rabbit():
    """Мок RabbitMQ — не нужен реальный сервер для тестов."""
    mock = AsyncMock()
    mock.publish = AsyncMock()
    return mock

@pytest.fixture
async def client(task_storage, mock_rabbit):
    """
    HTTP-клиент для тестирования.
    Подменяет реальные зависимости на моки.
    """
    app.dependency_overrides[get_rabbit_service] = lambda: mock_rabbit
    app.dependency_overrides[get_task_storage] = lambda: task_storage

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test"
    ) as client:
        yield client

    app.dependency_overrides.clear()
```

### Unit-тесты: TaskStorage

```python
# tests/test_task_storage.py
from app.services.task_storage import InMemoryTaskStorage
from app.schemas.task import TaskStatus

class TestTaskStorage:
    """Тесты хранилища задач — чистые unit-тесты без HTTP."""

    def test_create_task(self, task_storage: InMemoryTaskStorage):
        task_id = task_storage.create_task()

        assert task_id is not None
        task = task_storage.get_task(task_id)
        assert task.status == TaskStatus.PENDING

    def test_update_status(self, task_storage: InMemoryTaskStorage):
        task_id = task_storage.create_task()
        task_storage.update_status(task_id, TaskStatus.PROCESSING)

        task = task_storage.get_task(task_id)
        assert task.status == TaskStatus.PROCESSING

    def test_get_nonexistent_task(self, task_storage: InMemoryTaskStorage):
        task = task_storage.get_task("nonexistent-id")
        assert task is None
```

### Интеграционные тесты: Books API

```python
# tests/test_books.py

class TestBooksAPI:
    """Тесты эндпоинтов книг — используют HTTP-клиент."""

    async def test_create_book(self, client, mock_rabbit):
        """POST /books — создание книги и задачи."""
        response = await client.post("/books", json={
            "title": "Clean Code",
            "author": "Robert Martin"
        })

        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "Clean Code"
        assert "task_id" in data

        mock_rabbit.publish.assert_called_once()

    async def test_create_book_validation_error(self, client):
        """POST /books — невалидные данные."""
        response = await client.post("/books", json={
            "title": ""
        })

        assert response.status_code == 422

    async def test_get_books_empty(self, client):
        """GET /books — пустой список."""
        response = await client.get("/books")

        assert response.status_code == 200
        assert response.json() == []
```

### Тесты задач

```python
# tests/test_tasks.py

class TestTasksAPI:
    """Тесты эндпоинтов задач."""

    async def test_get_task_status(self, client, task_storage):
        """GET /tasks/{id} — получение статуса задачи."""
        task_id = task_storage.create_task()

        response = await client.get(f"/tasks/{task_id}")

        assert response.status_code == 200
        assert response.json()["status"] == "pending"

    async def test_get_task_not_found(self, client):
        """GET /tasks/{id} — несуществующая задача."""
        response = await client.get("/tasks/nonexistent")

        assert response.status_code == 404

    async def test_update_task_status(self, client, task_storage):
        """PUT /tasks/{id}/status — обновление статуса воркером."""
        task_id = task_storage.create_task()

        response = await client.put(
            f"/tasks/{task_id}/status",
            json={"status": "processing"},
            headers={"X-API-Key": "internal-secret-key"}
        )

        assert response.status_code == 200

    async def test_update_task_status_forbidden(self, client, task_storage):
        """PUT /tasks/{id}/status — без API-ключа."""
        task_id = task_storage.create_task()

        response = await client.put(
            f"/tasks/{task_id}/status",
            json={"status": "processing"}
        )

        assert response.status_code == 403
```

### Запуск тестов

```bash
# Все тесты
pytest

# С подробным выводом
pytest -v

# Конкретный файл
pytest tests/test_books.py

# Конкретный тест
pytest tests/test_books.py::TestBooksAPI::test_create_book

# С покрытием
pip install pytest-cov
pytest --cov=app --cov-report=term-missing
```

### Что делать

1. Установить `pytest`, `pytest-asyncio`, `httpx`
2. Создать `tests/conftest.py` с фикстурами
3. Написать unit-тесты для `TaskStorage`
4. Написать интеграционные тесты для `/books` и `/tasks`
5. Настроить `pytest.ini`
6. Запустить и убедиться что все тесты проходят
