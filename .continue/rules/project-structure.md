---
name: backend-stack
globs: "**/*.py"
---

# Стек проекта

- FastAPI + Pydantic v2
- SQLAlchemy 2.0 (async) + Alembic
- PostgreSQL через asyncpg
- Redis (кэш, pub/sub)
- RabbitMQ через aio-pika
- JWT через python-jose, хеши через passlib[bcrypt]

## Структура
- `app/routers/` — FastAPI endpoints
- `app/services/` — бизнес-логика
- `app/repositories/` — слой доступа к данным
- `app/models/` — SQLAlchemy ORM
- `app/schemas/` — Pydantic схемы
- `app/middleware/` — middleware
- `app/utils/` — вспомогательные функции
- `worker/` — фоновые обработчики (RabbitMQ consumer)

## План обучения
Этапы лежат в `docs/plans/stages/stage-XX-*.md`.
Готовые решения по этапам — в `hints/stage-XX/` (использовать только для проверки своей реализации).