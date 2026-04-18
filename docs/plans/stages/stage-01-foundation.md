# Этап 1: Фундамент проекта

### Цель

Поднять скелет приложения: структура каталогов, подключение PostgreSQL через SQLAlchemy async, Alembic-миграции, конфигурация, первая модель.

### Что появляется в проекте

- Структура каталогов по слоям (routers / services / repositories / models / schemas)
- Подключение к PostgreSQL через asyncpg + SQLAlchemy 2.0 async
- Alembic для управления миграциями
- Базовая модель с общими полями (id, created_at, updated_at)
- Конфигурация через pydantic-settings (.env)
- Health-check эндпоинт `GET /health` (проверка связи с БД)
- Lifespan: подключение/отключение от БД при старте/остановке

### Новые зависимости

```
sqlalchemy[asyncio]>=2.0
asyncpg
alembic
pydantic-settings
```

### Файлы для создания

| Файл | Назначение |
|------|-----------|
| `app/database.py` | AsyncEngine, async_session_maker, get_db dependency |
| `app/models/base.py` | DeclarativeBase, миксин TimestampMixin (created_at, updated_at) |
| `app/models/user.py` | Модель User (заглушка для первой миграции) |
| `app/repositories/base.py` | BaseRepository с generic CRUD |
| `app/config.py` | Settings: database_url, rabbitmq_url, и т.д. |
| `app/main.py` | FastAPI app, lifespan, health-check |
| `alembic.ini` | Конфигурация Alembic |
| `migrations/env.py` | Async-совместимый env для Alembic |
| `.env.example` | Пример переменных окружения |

### Ключевые концепции

- **SQLAlchemy 2.0 Mapped style** — `Mapped[str]`, `mapped_column()` вместо `Column()`
- **AsyncSession** — `async with async_session() as session`
- **Alembic autogenerate** — `alembic revision --autogenerate -m "init"`
- **Dependency Injection** — `get_db` как async generator для FastAPI Depends

### SQL-фокус

| Что изучаем | Как |
|------------|-----|
| CREATE TABLE, типы данных | Через модели SQLAlchemy → autogenerate миграция |
| Подключение к БД | asyncpg connection string, pool settings |
| Первая миграция | `alembic upgrade head` |

### Как проверить

1. `alembic upgrade head` — миграция применяется без ошибок
2. `uvicorn app.main:app --reload` — сервер стартует
3. `GET /health` — `{"status": "ok", "db": "connected"}`
4. В PostgreSQL видна таблица `users` (пока пустая)
