# Этап 14: Production-readiness

### Цель

Подготовить приложение к production: structured logging, reconnection, idempotency, security hardening, graceful shutdown.

> **CORS настраивается при появлении домена**, не на этом этапе. Progressive rate limiting уже реализован на этапе 2.

### Что появляется в проекте

#### 1. Structured Logging
- JSON-логирование для production (ELK/Loki-совместимое)
- Human-readable формат для разработки
- Контекстные поля: request_id, user_id, task_id
- Настройка уровней через конфиг
- Замена всех `print()` → `logger`

#### 2. Reconnection / Connection Recovery
- `aio_pika.connect_robust()` для RabbitMQ (автопереподключение)
- Reconnect callbacks для логирования
- Health-check для всех сервисов (`GET /health` проверяет БД + RabbitMQ + Redis)
- SQLAlchemy pool: `pool_pre_ping=True` для обнаружения разорванных соединений
- Redis: connection pool с reconnect

#### 3. Idempotency
- Idempotency-Key в заголовке для POST-запросов (защита от двойного клика)
- Проверка task_id перед обработкой в воркере (защита от redelivery)
- Хранение обработанных ID в Redis с TTL (быстрая проверка)

#### 4. Security Hardening
- Security headers: `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`
- Ревизия секретов: JWT_SECRET, RABBITMQ_PASSWORD, MINIO_SECRET_KEY — все из env, не хардкод
- Ревизия всех `print()` → убрать из production (только logger)
- Проверка что progressive ban работает корректно под нагрузкой

> **CORS** — настраивается при появлении домена (отдельный шаг перед деплоем).

#### 5. Graceful Shutdown
- Lifespan: корректное закрытие БД, RabbitMQ, Redis, WebSocket при SIGTERM
- Воркер: дообработка текущего сообщения перед остановкой
- APScheduler: graceful shutdown (finish running jobs)

### Файлы для создания / изменения

| Файл | Назначение |
|------|-----------|
| `app/logging_config.py` | JSONFormatter, setup_logging() |
| `app/config.py` | Добавить: log_json, log_level |
| `app/services/rabbit.py` | connect_robust, reconnect callbacks |
| `app/middleware/idempotency.py` | Idempotency-Key (Redis) |
| Ревизия всех файлов | Замена print() → logger, проверка секретов |

### Ключевые концепции

- **Structured logging** — JSON поля вместо строк, фильтрация в production
- **connect_robust** — автопереподключение с configurable interval
- **Idempotency** — Redis SET с TTL для дедупликации запросов
- **Graceful shutdown** — SIGTERM → stop accepting → finish current → close connections
- **Health check** — `/health` проверяет ВСЕ зависимости: PostgreSQL, RabbitMQ, Redis, MinIO

### Как проверить

1. `LOG_JSON=true` — логи в JSON формате
2. Перезапустить RabbitMQ → через 5 сек приложение переподключается
3. Отправить один и тот же запрос с `Idempotency-Key: abc` дважды → один результат
4. `GET /health` — проверка БД + RabbitMQ + Redis + MinIO
5. `docker stop rabbitmq && docker start rabbitmq` → воркер восстанавливается
6. `kill -SIGTERM <api_pid>` → текущие запросы завершаются, новые отклоняются
