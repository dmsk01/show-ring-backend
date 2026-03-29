# План обучения: FastAPI + RabbitMQ

## Цель проекта

Создать микросервисное приложение для изучения работы с очередями сообщений:
- **Backend:** FastAPI + aio-pika (асинхронный клиент RabbitMQ)
- **Worker:** Отдельный процесс для обработки задач из очереди
- **Frontend:** Vue.js для визуализации и тестирования
- **Broker:** RabbitMQ (установлен локально)

---

## Архитектура

```
┌─────────────┐     HTTP      ┌─────────────┐
│   Vue.js    │ ────────────> │   FastAPI   │
│  Frontend   │ <──────────── │   Backend   │
└─────────────┘               └──────┬──────┘
                                     │
                                     │ AMQP (publish)
                                     ▼
                              ┌─────────────┐
                              │  RabbitMQ   │
                              │   Broker    │
                              └──────┬──────┘
                                     │
                                     │ AMQP (consume)
                                     ▼
                              ┌─────────────┐
                              │   Worker    │
                              │  (Python)   │
                              └─────────────┘
```

### Структура проекта

```
my/
├── app/                        # FastAPI backend
│   ├── main.py                 # Точка входа, lifespan, CORS
│   ├── config.py               # Настройки из .env
│   ├── dependencies.py         # Dependency Injection
│   ├── exceptions.py           # Кастомные исключения
│   ├── routers/                # HTTP эндпоинты
│   │   ├── books.py            # CRUD книг
│   │   ├── tasks.py            # Работа с задачами
│   │   └── events.py           # Pub/Sub демонстрация
│   ├── services/               # Бизнес-логика
│   │   ├── rabbit.py           # Работа с RabbitMQ
│   │   └── task_storage.py     # Хранение статусов задач
│   └── schemas/                # Pydantic модели
│       ├── book.py
│       └── task.py
├── worker/                     # Обработчик очередей
│   ├── main.py                 # Точка входа воркера
│   ├── config.py               # Настройки воркера
│   └── handlers/
│       └── book_handler.py
├── frontend/                   # Vue.js приложение
├── docs/plans/                 # Документация (вы тут)
├── .env                        # Переменные окружения
└── requirements.txt
```

---

## Справочные материалы

- [Best Practices: Python & FastAPI](stages/best-practices.md) — типизация, Pydantic, DI, логирование, обработка ошибок, конфигурация
- [Ключевые концепции RabbitMQ](stages/rabbitmq-concepts.md) — термины, типы exchange, гарантии доставки
- [Production-Ready практики](stages/production-ready.md) — health checks, graceful shutdown, retry, DLQ, метрики

---

## Этапы обучения

### Часть 1: Основы RabbitMQ

| # | Этап | Статус | Документация |
|---|------|--------|-------------|
| 1 | Hello World с очередями | ✅ Завершён | [stage-01](stages/stage-01-hello-world.md) |
| 2 | Практический Task Queue | ✅ Завершён | [stage-02](stages/stage-02-task-queue.md) |
| 3 | Pub/Sub (Fanout Exchange) | | [stage-03](stages/stage-03-fanout.md) |
| 4 | Routing (Topic Exchange) | | [stage-04](stages/stage-04-topic.md) |

### Часть 2: Фронтенд и фреймворки

| # | Этап | Статус | Документация |
|---|------|--------|-------------|
| 5 | Vue фронтенд | | [stage-05](stages/stage-05-vue.md) |
| 6 | FastStream | | [stage-06](stages/stage-06-faststream.md) |

### Часть 3: Инженерные практики

| # | Этап | Статус | Документация |
|---|------|--------|-------------|
| 7 | Dependency Injection на практике | | [stage-07](stages/stage-07-dependency-injection.md) |
| 8 | Middleware в FastAPI | | [stage-08](stages/stage-08-middleware.md) |
| 9 | Тестирование (pytest + httpx) | | [stage-09](stages/stage-09-testing.md) |
| 10 | Structured Logging на практике | | [stage-10](stages/stage-10-logging.md) |
| 11 | Reconnection / Connection Recovery | | [stage-11](stages/stage-11-reconnection.md) |
| 12 | Idempotency | | [stage-12](stages/stage-12-idempotency.md) |

### Часть 4: Деплой

| # | Этап | Статус | Документация |
|---|------|--------|-------------|
| 13 | Docker Compose | | [stage-13](stages/stage-13-docker.md) |

---

## Чеклист готовности

[Полный чеклист по всем этапам](stages/checklist.md)
