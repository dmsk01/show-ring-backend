# ShowTail — backend

Платформа управления выставками животных. Стек: **FastAPI (async) · PostgreSQL · RabbitMQ · Redis · MinIO**, всё поднимается в Docker Compose.

Эта инструкция — как запустить backend локально, чтобы фронтенд (на этой же машине) мог с ним работать.

---

## Требования

- **Docker Desktop** (Windows/macOS) или Docker Engine + Docker Compose v2 (Linux). Проверка:
  ```powershell
  docker --version
  docker compose version
  ```
- ~2 ГБ свободной RAM под стек.

> ⚠️ **Порт 5432.** Если на машине стоит локальный PostgreSQL, он займёт 5432, и контейнер `postgres` не стартует. Останови локальную службу PG **или** поменяй в `docker-compose.yml` маппинг порта на `"5433:5432"`.

---

## Запуск за 3 шага

### 1. Создай `.env`
```powershell
Copy-Item .env.compose.example .env
```

### 2. Сгенерируй секреты и впиши их в `.env`
В `.env` поменяй `SECRET_KEY=change-me-in-production` на реальное значение и задай `INTERNAL_API_KEY`:
```powershell
python -c "import secrets; print(secrets.token_hex(32))"      # → SECRET_KEY
python -c "import secrets; print(secrets.token_urlsafe(48))"  # → INTERNAL_API_KEY
```
`.env` в git не коммитится (он в `.gitignore`) — это нормально.

### 3. Подними стек (dev-режим: авто-reload + MailPit)
```powershell
docker compose -f docker-compose.yml -f docker-compose.dev.yml --env-file .env up --build
```
Что произойдёт автоматически:
- поднимутся PostgreSQL, RabbitMQ, Redis, MinIO, MailPit;
- контейнер `migrate` накатит миграции (`alembic upgrade head`);
- контейнер `minio-init` создаст bucket;
- стартанут `api` (с `--reload`) и воркеры.

Первый `--build` идёт несколько минут (ставятся зависимости). Дальше — секунды.

Останов: `Ctrl+C`, затем (по желанию) `docker compose ... down`.

---

## Проверка, что бэкенд жив

- **Swagger / OpenAPI:** http://localhost:8000/docs
- **Health:** http://localhost:8000/health — JSON со статусом каждой зависимости (`ok`/`down`).

---

## Подключение фронтенда

Фронт на `http://localhost:5173` и API на `http://localhost:8000` — это **разные origin** (порт отличается), поэтому браузер применяет CORS. Два пути:

### Вариант A (рекомендую) — прокси дев-сервера, CORS не нужен
В конфиге фронта проксируй запросы на бэкенд. Пример для **Vite** (`vite.config.js`):
```js
export default {
  server: {
    proxy: { '/api': 'http://localhost:8000' },
  },
}
```
Тогда фронт дёргает `/api/...` на своём же origin, а Vite сам форвардит на бэкенд → CORS не возникает, в бэкенде ничего настраивать не надо.

### Вариант B — включить CORS на бэкенде
1. В `.env` добавь origin фронта (формат — JSON-массив):
   ```
   CORS_ALLOW_ORIGINS=["http://localhost:5173"]
   ```
2. Проброс переменной в контейнер `api`: в `docker-compose.yml` в блок `api.environment` добавь строку
   ```yaml
   CORS_ALLOW_ORIGINS: ${CORS_ALLOW_ORIGINS:-[]}
   ```
   (сейчас она туда не передаётся, поэтому без этого шага `.env` не подхватится).
3. Перезапусти: `docker compose ... up -d api`.

---

## Создание первого админа

После старта в БД нет ни одного пользователя с ролью admin. Регистрация через `/auth/register` создаёт обычного юзера. Чтобы получить админа:
```powershell
docker compose exec api python -m scripts.bootstrap_admin --email admin@example.com --password ChangeMe123!
```
Скрипт идемпотентный — повторный запуск безопасен.

---

## Тестовые пользователи для e2e

Идемпотентный сид аккаунтов под Playwright-тесты фронта (повторный запуск безопасен, дублей не плодит):
```powershell
docker compose exec api python -m scripts.seed_e2e_users --force
```
Пароль у всех: **`Password123!`**. Email подтверждён, аккаунт активен — `/auth/login` работает сразу.

| Email | Роли |
|---|---|
| organizer@e2e.example | organizer |
| breeder@e2e.example | breeder |
| judge@e2e.example | judge |
| buyer@e2e.example | buyer |
| operator@e2e.example | operator |
| multi@e2e.example | breeder + organizer (проверка union прав) |

> Домен `@e2e.example`, а не `@e2e.test`: pydantic `EmailStr` отвергает зарезервированный TLD `.test`, и `/auth/login` отвечал бы 422 ещё до проверки пароля. `.example` тоже зарезервирован под тесты (RFC 2606) — письма туда не уйдут.

Пользователя с ролью admin сид не создаёт — используй существующего админа (см. «Создание первого админа» выше).

---

## Полезные адреса (dev)

| Сервис | URL | Доступ |
|---|---|---|
| API (Swagger) | http://localhost:8000/docs | — |
| Health | http://localhost:8000/health | — |
| RabbitMQ UI | http://localhost:15672 | guest / guest |
| MinIO Console | http://localhost:9001 | из `.env` (S3_ACCESS_KEY / S3_SECRET_KEY) |
| MailPit (письма) | http://localhost:8025 | — |

---

## Частые команды

```powershell
# логи только API
docker compose logs -f api

# перезапустить один сервис (после правок в .env)
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d api

# остановить всё (данные в volume сохраняются)
docker compose down

# остановить и СТЕРЕТЬ все данные (чистый старт)
docker compose down -v
```

> В dev-режиме код примонтирован в контейнер: правки в `app/` подхватываются `--reload` у `api` автоматически. Воркеры reload не умеют — после правок их кода перезапусти: `docker compose restart worker worker-files`.

---

## Миграции

Накатываются автоматически контейнером `migrate` при каждом `up`. Вручную (если меняешь схему):
```powershell
docker compose exec api alembic upgrade head      # применить
docker compose exec api alembic revision --autogenerate -m "описание"  # создать
```

---

## Обновление кода и устранение неполадок

**После `git pull` нового кода бэкенда — пересобирай образы:**
```powershell
docker compose -f docker-compose.yml -f docker-compose.dev.yml --env-file .env up -d --build
```
Без `--build` Docker оставит старые образы — частая причина рассинхрона кода и миграций (`alembic ... Can't locate revision ...`).

**Стек сам поднимается после перезапуска Docker.** У всех сервисов задан `restart: unless-stopped`, поэтому после ребута Docker Desktop/машины контейнеры стартуют автоматически.

**Если `/health/` отдаёт 503 или таймаут** — обычно отвалилась зависимость. Смотри статусы:
```powershell
docker ps -a --filter "name=show-ring-backend" --format "table {{.Names}}\t{{.Status}}"
```
Если какой-то сервис в `Exited` — подними стек заново:
```powershell
docker compose -f docker-compose.yml -f docker-compose.dev.yml --env-file .env up -d
```
Логи конкретного сервиса (где смотреть ошибку): `docker compose logs --tail=50 api` (или `postgres`, `migrate`, …).

---

## Тесты (опционально)

Юнит-тесты идут без инфраструктуры; интеграционные требуют поднятых PostgreSQL и Redis (берутся из запущенного стека). Запуск — в локальном venv:
```powershell
.\venv\Scripts\python.exe -m pytest -q
```
