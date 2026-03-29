# Этап 13: Docker Compose

### Цель

Упаковать всё приложение (API + Worker + RabbitMQ) в Docker Compose. Один `docker-compose up` — и всё работает.

### Почему Docker Compose

- **Воспроизводимость** — работает одинаково на любой машине
- **Изоляция** — не нужно ставить RabbitMQ локально
- **Приближение к production** — в реальности сервисы работают в контейнерах
- **Onboarding** — новый разработчик запускает проект одной командой

### Структура файлов

```
my/
├── app/
├── worker/
├── Dockerfile              # Один образ для API и Worker
├── docker-compose.yml
├── .env
└── requirements.txt
```

### Dockerfile

```dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### docker-compose.yml

```yaml
services:
  rabbitmq:
    image: rabbitmq:3-management
    ports:
      - "5672:5672"     # AMQP
      - "15672:15672"   # Management UI
    environment:
      RABBITMQ_DEFAULT_USER: guest
      RABBITMQ_DEFAULT_PASS: guest
    healthcheck:
      test: ["CMD", "rabbitmq-diagnostics", "check_port_connectivity"]
      interval: 10s
      timeout: 5s
      retries: 5

  api:
    build: .
    ports:
      - "8000:8000"
    environment:
      RABBITMQ_URL: amqp://guest:guest@rabbitmq/
      LOG_JSON: "false"
    depends_on:
      rabbitmq:
        condition: service_healthy
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

  worker:
    build: .
    environment:
      RABBITMQ_URL: amqp://guest:guest@rabbitmq/
      API_URL: http://api:8000
      LOG_JSON: "false"
    depends_on:
      rabbitmq:
        condition: service_healthy
      api:
        condition: service_started
    command: python -m worker.main
```

### Ключевые концепции

#### 1. Сеть в Docker Compose

Все сервисы в одном `docker-compose.yml` находятся в общей сети. Обращаются друг к другу по имени сервиса:

```
api -> rabbitmq:5672      (не localhost!)
worker -> rabbitmq:5672
worker -> api:8000
```

#### 2. depends_on + healthcheck

`depends_on` без `condition` — только гарантирует порядок запуска контейнеров, но **не** ждёт готовности сервиса.

```yaml
# ❌ Плохо — API стартует раньше чем RabbitMQ готов
depends_on:
  - rabbitmq

# ✅ Хорошо — ждёт пока healthcheck пройдёт
depends_on:
  rabbitmq:
    condition: service_healthy
```

#### 3. Hot Reload в Docker

```yaml
api:
  volumes:
    - .:/app  # Монтирует локальный код в контейнер
  command: uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

#### 4. .dockerignore

```
.venv/
__pycache__/
*.pyc
.env
.git/
docs/
```

### Команды

```bash
# Запуск всего стека
docker-compose up

# Запуск в фоне
docker-compose up -d

# Пересборка после изменений в Dockerfile/requirements.txt
docker-compose up --build

# Просмотр логов
docker-compose logs -f api
docker-compose logs -f worker

# Остановка
docker-compose down

# Остановка с удалением volumes (очистка данных RabbitMQ)
docker-compose down -v

# Масштабирование воркеров
docker-compose up --scale worker=3
```

### Как проверить

1. `docker-compose up --build`
2. Открыть RabbitMQ Management: `http://localhost:15672` (guest/guest)
3. Создать книгу: `curl -X POST http://localhost:8000/books -H "Content-Type: application/json" -d '{"title": "Test", "author": "Author"}'`
4. Проверить статус: `curl http://localhost:8000/tasks/{task_id}`
5. Логи воркера: `docker-compose logs -f worker`

### Что делать

1. Создать `Dockerfile`
2. Создать `docker-compose.yml`
3. Создать `.dockerignore`
4. Запустить `docker-compose up --build`
5. Проверить полный цикл: создание книги -> обработка воркером -> получение статуса
6. Попробовать `--scale worker=2` и убедиться, что задачи распределяются
