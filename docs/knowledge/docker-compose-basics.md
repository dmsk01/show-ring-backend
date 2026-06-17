# Docker Compose — образы, контейнеры, сеть

Как устроен стек ShowTail в Docker Compose: что скачивается, что собирается, как сервисы находят друг друга, и какими командами это всё инспектировать и чистить.

## Образ vs контейнер

Это первое, что нужно развести.

- **Образ (image)** — read-only «снимок» файловой системы + рецепт. Сам ничего не делает, просто лежит на диске. Аналогия: класс в Python.
- **Контейнер (container)** — запущенный экземпляр образа, живой процесс. Аналогия: объект класса.

Из одного образа можно поднять сколько угодно контейнеров. Контейнер — это **не виртуалка**: все контейнеры делят ядро хост-машины (через Docker Desktop), внутри нет отдельной ОС. Поэтому они лёгкие.

## image vs build — скачать или собрать

В `docker-compose.yml` сервисы делятся на две группы.

**Готовые образы с Docker Hub** — строка `image:`:

```yaml
postgres:  image: postgres:17-alpine
redis:     image: redis:8-alpine
rabbitmq:  image: rabbitmq:3-management-alpine
minio:     image: minio/minio
```

Их собрали авторы Postgres/Redis и выложили в публичный реестр — мы просто качаем готовое.

**Свои образы из `Dockerfile`** — строка `build:`:

```yaml
api:     build: .
worker:  build: .
migrate: build: .
```

`build: .` = «собери образ по `Dockerfile` из этой папки». Внутрь кладётся наш Python-код.

## Один Dockerfile — несколько ролей

В ShowTail `api`, `worker`, `worker-files`, `migrate` собираются из **одного** `Dockerfile` (один рецепт). Но это **не один контейнер** — это отдельные контейнеры, каждый свой процесс. Различаются командой запуска `command:`:

```yaml
api:     command: uvicorn app.main:app ...        # веб-сервер
worker:  command: python -m worker.main --mode documents
migrate: command: alembic upgrade head           # накатил миграции и завершился
```

Один «диск с программой», запущенный по-разному под разные роли. Postgres/Redis/Rabbit/MinIO — у каждого свой отдельный образ и контейнер.

## Сеть и DNS по именам сервисов

Compose при старте создаёт **одну виртуальную сеть** и подключает к ней все контейнеры. Внутри работает DNS: **имя сервиса = сетевой адрес.** Поэтому в конфиге `api`:

```yaml
DATABASE_URL: postgresql+asyncpg://...@postgres:5432/...
REDIS_URL:    redis://redis:6379/0
S3_ENDPOINT:  http://minio:9000
```

`postgres`, `redis`, `minio` — не хосты в интернете, а **имена сервисов** из compose. Контейнер стучится по имени соседа, Docker направляет трафик в нужный контейнер. IP-адреса не нужны.

`ports:` отдельная история — это проброс наружу, на хост (`8000:8000` → API доступен с localhost:8000). Между собой контейнеры общаются по внутренней сети и без проброса портов.

## Картинка целиком

```
Хост (Docker Desktop)   порты наружу: 8000→api  5432→postgres  15672→rabbit
┌──────────────────────────────────────────────────┐
│  Сеть Compose (DNS по именам сервисов)             │
│   [api]  [worker]  [worker-files]  ← наш Dockerfile│
│      \      |        /               (build: .)    │
│   [postgres] [redis] [rabbitmq] [minio]            │
│      ↑ готовые образы с Docker Hub (image:)        │
└──────────────────────────────────────────────────┘
Каждый блок = отдельный контейнер = отдельный процесс.
Связаны не «в один образ», а общей сетью + обращением по имени.
```

## Доступ к ресурсам из консоли

Статус всех сервисов проекта:

```bash
docker compose ps
```

Логи конкретного сервиса (live, с хвоста):

```bash
docker compose logs -f api
```

Шелл внутрь контейнера (в нашем образе есть `bash` и `curl`):

```bash
docker compose exec api bash
```

Проверка бэкенда — снаружи (порт проброшен) и изнутри сети:

```bash
curl -L http://localhost:8000/health        # детальный статус компонентов (-L: /health 307-редиректит на /health/)
curl http://localhost:8000/docs             # Swagger UI в браузере
docker compose exec api curl -fsS http://localhost:8000/health/ready   # readiness изнутри контейнера (как HEALTHCHECK)
```

В ShowTail два health-эндпоинта: `/health/` всегда отдаёт 200 с разбивкой по
компонентам (для дашборда), а `/health/ready` — бинарный readiness (200 если PG
жив, 503 если БД down). `HEALTHCHECK` контейнера (см. `Dockerfile`) бьёт именно
в `/health/ready` — он и помечает контейнер unhealthy при падении БД. Подробнее —
`docs/knowledge/fastapi-lifespan-healthcheck.md`.

Подключиться к БД внутри контейнера:

```bash
docker compose exec postgres psql -U showtail -d showtail
```

Проверить статус healthcheck отдельного контейнера:

```bash
docker inspect --format '{{.State.Health.Status}}' show-ring-backend-worker-1
```

## Осиротевшие контейнеры и очистка

**Важно:** при `docker compose up` Compose **переиспользует имена** сервисов — пересоздаёт контейнер с тем же именем, а не плодит новые. Поэтому контейнеры от Compose не «скапливаются». Реально копится другое: **кэш сборки** и **старые образы** после каждого `--build`.

Диагностика — что и сколько занимает:

```bash
docker system df
```

Осиротевший контейнер — это сервис, удалённый из compose-файла, либо запущенный вручную `docker run`. Убрать:

```bash
docker compose down --remove-orphans   # гасит стек и чистит сервисы, которых уже нет в YAML
docker rm <имя_контейнера>             # точечно удалить конкретный мёртвый контейнер
```

Очистка мусора:

```bash
docker builder prune    # только кэш сборки (обычно самый большой источник мусора)
docker image prune       # висячие образы (тег <none> после пересборки)
docker image prune -a    # + все тегированные образы, не привязанные к контейнеру
docker system prune      # всё вместе: стопнутые контейнеры + dangling-образы + кэш + сети
```

⚠️ **Что НЕ делать бездумно:** `docker volume prune`, `docker system prune --volumes`, `docker compose down -v` — удаляют тома (`postgres_data` и пр.), а с ними и данные БД. Тома существуют ровно чтобы данные пережили `down`; чистить их — только осознанно.

Чтобы мусор не копился: не делай `--build`, когда сборка не нужна. На dev код подключён через bind-mount (`./app:/app/app`) + `--reload`, образ пересобирать нужно только при изменении `requirements.txt` или самого `Dockerfile`.

## Ссылки

- [Docker Compose — обзор](https://docs.docker.com/compose/)
- [Compose networking](https://docs.docker.com/compose/how-tos/networking/)
- [docker compose CLI](https://docs.docker.com/reference/cli/docker/compose/)
- [Пересоздание ресурсов и `prune`](https://docs.docker.com/engine/manage-resources/pruning/)
