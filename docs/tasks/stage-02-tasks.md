# Этап 2: Пользователи и авторизация — задачи

> Статус: большинство кода написано. Осталось подключить rate limiting и
> провести сквозную проверку.

---

## Задача 2.1 — Подключить rate limiting к auth-эндпоинтам

### 1. Что делать

Файл: `app/routers/auth.py`

Импортировать `check_rate_limit` из `app.middleware.progressive_ban` и
`get_redis` из `app.redis`. Добавить вызов `check_rate_limit` в начало
каждого из трёх публичных auth-эндпоинтов:

| Эндпоинт | limit | window |
|----------|-------|--------|
| `POST /auth/login` | 5 | 60 |
| `POST /auth/register` | 3 | 3600 |
| `POST /auth/verify-email` | 10 | 60 |

### 2. Как это работает

`check_rate_limit` — это async-функция в `progressive_ban.py`. Она
не является ASGI-middleware (не оборачивает всё приложение), а вызывается
точечно внутри конкретного обработчика. Функция принимает `Request`,
строку-идентификатор эндпоинта, лимит, окно и объект `Redis`.
Если лимит превышен — поднимает `HTTPException(429)` с заголовком
`Retry-After`. Если нет — просто возвращает `None`.

`get_redis` — зависимость FastAPI, возвращает `Redis` из пула,
инициализированного при старте приложения в `lifespan`.

### 3. API технологии / примеры

```python
from app.middleware.progressive_ban import check_rate_limit
from app.redis import get_redis
from redis.asyncio import Redis

@router.post("/login")
async def login(
    request: Request,
    body: UserCreate,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    await check_rate_limit(request, "/auth/login", limit=5, window=60, redis=redis)
    ...
```

`Request` нужен, чтобы `check_rate_limit` мог получить IP клиента через
`request.client.host`.

### 4. Зачем это нужно

Без rate limiting брутфорс паролей ничем не ограничен — можно отправлять
`POST /auth/login` тысячи раз в секунду. Функция уже написана и
протестирована; задача — именно вызов из нужных мест. Общий API-лимит
(60 req/min) пока не требуется — его добавим на этапе оптимизации.

### 5. Ключевые термины / функции

- `check_rate_limit(request, endpoint, limit, window, redis)` — проверяет
  IP против sliding window, кидает 429 при превышении
- `Request` — объект FastAPI/Starlette, содержит `request.client.host`
- `Depends(get_redis)` — инжектирует Redis-соединение через DI
- `Retry-After` — HTTP-заголовок, сообщающий клиенту через сколько секунд
  повторить запрос (RFC 6585)

### 6. Как проверить

Запустить сервер, затем отправить 6 запросов подряд на `/auth/login`:

```bash
for i in {1..6}; do
  curl -s -o /dev/null -w "%{http_code}\n" \
    -X POST http://localhost:8000/auth/login \
    -H "Content-Type: application/json" \
    -d '{"email":"x@x.com","password":"wrong"}'
done
```

Первые 5 — `401` (неверный пароль). Шестой — `429`.
Повторить сразу — `429` с `Retry-After: 4` (экспоненциальный рост).

---

## Задача 2.2 — Сквозная проверка всего этапа

### 1. Что делать

Запустить `docker compose up -d`, применить миграции и пройти все
сценарии вручную через `curl` или Swagger UI (`/docs`).

### 2. Как это работает

FastAPI автоматически генерирует Swagger UI на `/docs`. Там можно
выполнять запросы без curl — удобно для проверки авторизованных
эндпоинтов через кнопку "Authorize".

Alembic применяет миграции командой `upgrade head` — она выполняет
все `.py` файлы из `migrations/versions/` по порядку ревизий.

### 3. API технологии / примеры

```bash
# Применить миграции
alembic upgrade head

# Регистрация
curl -X POST http://localhost:8000/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"test@test.com","password":"secret123"}'

# Смотрим токен верификации в логах сервера, затем:
curl -X POST "http://localhost:8000/auth/verify-email?token=<TOKEN>"

# Логин
curl -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"test@test.com","password":"secret123"}'
```

### 4. Зачем это нужно

Юнит-тесты не заменяют сквозную проверку: JWT-зависимости,
Redis-соединение и PostgreSQL могут работать по отдельности, но
сломаться вместе. Этот шаг закрывает этап 2 официально.

### 5. Ключевые термины / функции

- `alembic upgrade head` — применить все pending-миграции
- `Authorization: Bearer <token>` — HTTP-заголовок для передачи JWT
- `401 Unauthorized` — токен отсутствует или невалиден
- `403 Forbidden` — токен валиден, но роль не подходит
- `429 Too Many Requests` — превышен rate limit

### 6. Как проверить

Пройти все 9 сценариев из `docs/plans/stages/stage-02-auth.md`
раздел "Как проверить". Все должны дать ожидаемый HTTP-статус.
