# Этап 15: Docker и деплой

### Цель

Упаковать всё приложение в Docker Compose: API + Worker + PostgreSQL + RabbitMQ + Redis + MinIO + MailPit. Один `docker-compose up` — и всё работает.

### Что появляется в проекте

- Dockerfile (multi-stage build)
- docker-compose.yml (все сервисы)
- docker-compose.dev.yml (override для разработки)
- .dockerignore
- Health checks для всех сервисов
- Масштабирование воркеров (`--scale worker=3`)
- Volumes для PostgreSQL данных и MinIO файлов
- **Init-контейнер для миграций** (исключает race condition при scale api=N)

### Файлы для создания

| Файл | Назначение |
|------|-----------|
| `Dockerfile` | Multi-stage: builder + runtime |
| `docker-compose.yml` | Все сервисы |
| `docker-compose.dev.yml` | Override для разработки (volumes, reload, mailpit) |
| `.dockerignore` | Исключения |
| `scripts/entrypoint.sh` | Health-wait + запуск |

### docker-compose.yml

```yaml
services:
  postgres:
    image: postgres:17
    environment:
      POSTGRES_DB: showtail
      POSTGRES_USER: showtail
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    ports:
      - "5432:5432"
    volumes:
      - postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U showtail"]
      interval: 5s
      timeout: 3s
      retries: 5

  rabbitmq:
    image: rabbitmq:3-management
    ports:
      - "5672:5672"
      - "15672:15672"
    environment:
      RABBITMQ_DEFAULT_USER: ${RABBITMQ_USER}
      RABBITMQ_DEFAULT_PASS: ${RABBITMQ_PASSWORD}
    healthcheck:
      test: ["CMD", "rabbitmq-diagnostics", "check_port_connectivity"]
      interval: 10s
      timeout: 5s
      retries: 5

  redis:
    image: redis:8-alpine
    ports:
      - "6379:6379"
    volumes:
      - redis_data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 5

  minio:
    image: minio/minio
    ports:
      - "9000:9000"
      - "9001:9001"
    environment:
      MINIO_ROOT_USER: ${MINIO_ACCESS_KEY}
      MINIO_ROOT_PASSWORD: ${MINIO_SECRET_KEY}
    volumes:
      - minio_data:/data
    command: server /data --console-address ":9001"
    healthcheck:
      test: ["CMD", "mc", "ready", "local"]
      interval: 10s
      timeout: 5s
      retries: 5

  # Отдельный контейнер для миграций — запускается ОДИН РАЗ перед API
  migrate:
    build: .
    environment:
      DATABASE_URL: postgresql+asyncpg://showtail:${POSTGRES_PASSWORD}@postgres/showtail
    depends_on:
      postgres:
        condition: service_healthy
    command: alembic upgrade head
    restart: "no"

  api:
    build: .
    ports:
      - "8000:8000"
    environment:
      DATABASE_URL: postgresql+asyncpg://showtail:${POSTGRES_PASSWORD}@postgres/showtail
      RABBITMQ_URL: amqp://${RABBITMQ_USER}:${RABBITMQ_PASSWORD}@rabbitmq/
      REDIS_URL: redis://redis:6379/0
      MINIO_ENDPOINT: minio:9000
      MINIO_ACCESS_KEY: ${MINIO_ACCESS_KEY}
      MINIO_SECRET_KEY: ${MINIO_SECRET_KEY}
      SMTP_HOST: mailpit
      SMTP_PORT: 1025
      JWT_SECRET: ${JWT_SECRET}
      LOG_JSON: "true"
    depends_on:
      migrate:
        condition: service_completed_successfully
      rabbitmq:
        condition: service_healthy
      redis:
        condition: service_healthy
      minio:
        condition: service_healthy
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000

  worker:
    build: .
    environment:
      DATABASE_URL: postgresql+asyncpg://showtail:${POSTGRES_PASSWORD}@postgres/showtail
      RABBITMQ_URL: amqp://${RABBITMQ_USER}:${RABBITMQ_PASSWORD}@rabbitmq/
      REDIS_URL: redis://redis:6379/0
      MINIO_ENDPOINT: minio:9000
      MINIO_ACCESS_KEY: ${MINIO_ACCESS_KEY}
      MINIO_SECRET_KEY: ${MINIO_SECRET_KEY}
      LOG_JSON: "true"
    depends_on:
      migrate:
        condition: service_completed_successfully
      rabbitmq:
        condition: service_healthy
      redis:
        condition: service_healthy
    command: python -m worker.main

  frontend:
    build: ./frontend
    ports:
      - "3000:3000"
    environment:
      NEXT_PUBLIC_API_URL: http://api:8000
    depends_on:
      - api

volumes:
  postgres_data:
  redis_data:
  minio_data:
```

### docker-compose.dev.yml (override для разработки)

```yaml
services:
  mailpit:
    image: axllent/mailpit
    ports:
      - "8025:8025"   # Web UI для просмотра писем
      - "1025:1025"   # SMTP
    healthcheck:
      test: ["CMD", "wget", "-q", "--spider", "http://localhost:8025"]
      interval: 10s
      timeout: 3s
      retries: 3

  api:
    volumes:
      - .:/app        # Live reload
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
    environment:
      LOG_JSON: "false"
    depends_on:
      mailpit:
        condition: service_healthy
```

### Ключевые концепции

- **Multi-stage build** — builder (pip install) + runtime (slim image)
- **Init-контейнер для миграций** — `migrate` сервис с `restart: "no"` и `service_completed_successfully`. Запускается один раз, API ждёт его завершения. Исключает race condition при `--scale api=3`
- **Health checks** — depends_on с condition: service_healthy
- **Volumes** — persistence для PostgreSQL, Redis и MinIO
- **Environment variables** — секреты через .env, не хардкод
- **Scaling** — `docker-compose up --scale worker=3`
- **Networking** — сервисы обращаются друг к другу по имени (postgres, rabbitmq, redis, minio)
- **MailPit** — только в dev (docker-compose.dev.yml), в prod — внешний SMTP

### Как проверить

1. `docker-compose -f docker-compose.yml -f docker-compose.dev.yml up --build` — всё поднимается
2. `curl http://localhost:8000/health` — API + DB + RabbitMQ + Redis + MinIO ok
3. `http://localhost:15672` — RabbitMQ Management UI
4. `http://localhost:9001` — MinIO Console
5. `http://localhost:8025` — MailPit UI (просмотр отправленных email)
6. Полный цикл: создать выставку → записать собаку → ввести результаты → сгенерировать диплом
7. `docker-compose up --scale worker=3` — задачи распределяются между воркерами
8. `docker-compose down && docker-compose up` — данные сохранились (volumes)
