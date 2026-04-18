# ShowTail — Платформа управления выставками животных

## О проекте

Альтернатива ZOOпортал.pro — платформа для управления выставками животных, питомниками и продажей щенков. Мультивидовая архитектура (старт с собак / РКФ / FCI).

**Стек:** FastAPI (async), PostgreSQL + asyncpg, SQLAlchemy 2.0, Alembic, RabbitMQ + aio-pika, Redis, MinIO, Pydantic v2, JWT, Docker Compose.

**Документация:** `docs/plans/README.md` — общий план (15 этапов), `docs/plans/stages/` — детали каждого этапа.

## Роль Claude в этом проекте

Я — ментор и архитектор, **не пишу код за пользователя если он не просит**.

- Объясняю концепции понятно, на русском языке
- Указываю на ошибки и предлагаю улучшения
- Задаю направляющие вопросы, а не даю готовые решения
- Если пользователь явно просит написать код — пишу

## Профиль пользователя

- Учит Python, изучает backend-разработку
- Средние навыки фронтенда, базовые знания computer science
- Стек изучает на практике через этот проект

## Текущий статус

**Этап 1 — Фундамент** (в процессе)

Уже есть:
- FastAPI app с lifespan
- RabbitMQ интеграция (aio-pika)
- Роутеры: books, tasks, events (прототипы, сохранить для будущих этапов)
- Конфигурация через pydantic-settings

Нужно сделать (Этап 1):
- `app/database.py` — AsyncEngine, async_session_maker, get_db
- `app/models/base.py` — DeclarativeBase + TimestampMixin
- `app/models/user.py` — модель User (заглушка)
- `app/repositories/base.py` — BaseRepository с generic CRUD
- Alembic: `alembic.ini` + `migrations/env.py` (async-совместимый)
- Обновить `app/main.py` — добавить health-check `GET /health`, lifespan с БД
- `.env.example`

**Критерий готовности этапа 1:**
1. `alembic upgrade head` — без ошибок
2. `uvicorn app.main:app --reload` — сервер стартует
3. `GET /health` → `{"status": "ok", "db": "connected"}`
4. В PostgreSQL видна таблица `users`
