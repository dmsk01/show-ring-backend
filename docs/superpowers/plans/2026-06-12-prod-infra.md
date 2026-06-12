# Прод-инфраструктура (бэкапы, nginx/TLS, prod-compose) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Довести ShowTail до деплоя на один VPS: nginx как единственный вход (80/443, edge rate-limit, WS), backup-сервис (pg_dump + mc mirror + ротация + опциональный offsite-S3), prod-оверлей compose с образами из registry, обновлённые deploy-стадии CI.

**Architecture:** Базовый `docker-compose.yml` становится прод-безопасным (без host-портов), dev-порты переезжают в `docker-compose.dev.yml`. Новый `docker-compose.prod.yml` добавляет nginx/certbot/backup и заменяет `build: .` на образы из ghcr. Бэкапы — отдельный контейнер с busybox crond.

**Tech Stack:** Docker Compose v2, nginx:1.27-alpine, certbot, postgres:17-alpine (pg_dump), MinIO client (mc), bash.

**Spec:** `docs/superpowers/specs/2026-06-12-prod-infra-design.md`

**Замечание по верификации:** это инфра-код — классический TDD неприменим. Каждая задача завершается проверочной командой с ожидаемым выводом; финальная задача 8 — приёмка из спеки (включая restore-учение).

---

### Task 1: Инверсия портов — база без host-портов, dev добавляет

**Files:**
- Modify: `docker-compose.yml` (services postgres, rabbitmq, redis, minio, api — убрать `ports:`)
- Modify: `docker-compose.dev.yml` (те же сервисы — добавить `ports:`)

- [ ] **Step 1: Убрать `ports:` из базового compose**

В `docker-compose.yml` удалить блоки `ports:` у `postgres`, `rabbitmq`, `redis`, `minio`, `api`. Комментарий у postgres про «Локальный порт открыт ради удобства dev-инструментов» заменить на:

```yaml
    # Host-порты НЕ публикуем: база прод-безопасна по умолчанию.
    # Dev-порты (psql/DBeaver и т.п.) добавляет docker-compose.dev.yml.
```

Аналогично убрать комментарий `# Management UI — http://localhost:15672 (guest/guest).` у rabbitmq и `# S3 API` / `# Web Console` у minio (они переедут в dev-файл вместе с портами).

- [ ] **Step 2: Добавить порты в dev-override**

В `docker-compose.dev.yml` в существующие сервисы добавить, а отсутствующие (postgres, rabbitmq, redis, minio) — создать:

```yaml
  # Dev-порты инфры: в базовом docker-compose.yml host-порты не публикуются
  # (прод-безопасный дефолт), наружу их выставляет только dev-override.
  postgres:
    ports:
      - "5432:5432"   # psql / DBeaver

  rabbitmq:
    ports:
      - "5672:5672"
      - "15672:15672" # Management UI — http://localhost:15672 (guest/guest)

  redis:
    ports:
      - "6379:6379"

  minio:
    ports:
      - "9000:9000"   # S3 API
      - "9001:9001"   # Web Console
```

В существующий сервис `api` dev-оверрайда добавить:

```yaml
    ports:
      - "8000:8000"   # прямой доступ к API без прокси (только dev)
```

- [ ] **Step 3: Проверить валидность обеих конфигураций**

```powershell
docker compose -f docker-compose.yml config --quiet
docker compose -f docker-compose.yml -f docker-compose.dev.yml config --format json | python -c "import json,sys; svc=json.load(sys.stdin)['services']; print({k: v.get('ports') for k,v in svc.items() if v.get('ports')})"
```

Ожидаемо: первая команда молчит (валидно); вторая показывает ports только из dev-файла (5432, 5672/15672, 6379, 9000/9001, 8000).

- [ ] **Step 4: Commit**

```bash
git add docker-compose.yml docker-compose.dev.yml
git commit -m "feat(deploy): инверсия портов — база без host-портов, dev-override добавляет"
```

---

### Task 2: Конфиги nginx

**Files:**
- Create: `deploy/nginx/nginx.conf`
- Create: `deploy/nginx/conf.d/showtail.conf`
- Create: `deploy/nginx/snippets/proxy.conf`

- [ ] **Step 1: Создать `deploy/nginx/nginx.conf`**

```nginx
# Главный конфиг nginx для ShowTail (прод, один VPS).
# Зоны limit_req и map для WebSocket обязаны жить в http-контексте —
# поэтому свой nginx.conf, а не только conf.d.

user nginx;
worker_processes auto;
error_log /var/log/nginx/error.log warn;
pid /var/run/nginx.pid;

events {
    worker_connections 1024;
}

http {
    include /etc/nginx/mime.types;
    default_type application/octet-stream;
    sendfile on;
    keepalive_timeout 65;

    # --- Edge rate limit (первая линия; вторая — progressive_ban.py) ---
    # Ключ — IP клиента. 10m зоны ~ 160k IP, хватит с запасом.
    limit_req_zone $binary_remote_addr zone=general:10m rate=20r/s;
    limit_req_zone $binary_remote_addr zone=auth:10m rate=5r/s;
    # По умолчанию nginx отдаёт 503 — выставляем честный 429.
    limit_req_status 429;

    # --- WebSocket upgrade (есть /ws/notifications и /support/ws/{id}) ---
    # Если клиент прислал Upgrade — пробрасываем, иначе закрываем
    # Connection (обычный HTTP keep-alive до upstream'а).
    map $http_upgrade $connection_upgrade {
        default upgrade;
        ''      close;
    }

    # Зеркалит max_upload_size_bytes приложения (10 МБ) — defence-in-depth.
    client_max_body_size 10m;

    include /etc/nginx/conf.d/*.conf;
}
```

- [ ] **Step 2: Создать `deploy/nginx/snippets/proxy.conf`**

```nginx
# Общие proxy-заголовки для всех location'ов.
# X-Forwarded-For/Proto читает app/middleware/proxy_headers.py —
# он доверяет им, только если peer входит в FORWARDED_ALLOW_IPS
# (в prod-compose это подсеть docker-сети 172.28.0.0/16).
proxy_set_header Host $host;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
# HTTP/1.1 + Upgrade — нужно для WebSocket (map в nginx.conf).
proxy_http_version 1.1;
proxy_set_header Upgrade $http_upgrade;
proxy_set_header Connection $connection_upgrade;
# WS-соединения уведомлений живут долго и молчат; дефолтные 60s
# рвали бы их. 3600s — компромисс (приложение само закрывает мёртвые).
proxy_read_timeout 3600s;
```

- [ ] **Step 3: Создать `deploy/nginx/conf.d/showtail.conf`**

```nginx
# Виртуальный хост ShowTail. Сейчас работает только :80 (домена нет).
# При появлении домена: см. блок 443 внизу + docs/knowledge/backups.md
# (раздел деплоя) — заменить showtail.example на домен, выпустить
# сертификат, раскомментировать.

upstream showtail_api {
    server api:8000;
}

server {
    listen 80;
    server_name _;

    # ACME-челлендж certbot'а (активен, когда появится домен).
    # Должен идти ДО общих location'ов и БЕЗ rate-limit.
    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    # Auth — жёсткая зона: credential stuffing и SMS pumping режем
    # на edge, до Python. nodelay — не выстраивать очередь, отбивать сразу.
    location /auth/ {
        limit_req zone=auth burst=10 nodelay;
        include /etc/nginx/snippets/proxy.conf;
        proxy_pass http://showtail_api;
    }

    # Всё остальное — общая зона.
    location / {
        limit_req zone=general burst=40 nodelay;
        include /etc/nginx/snippets/proxy.conf;
        proxy_pass http://showtail_api;
    }
}

# --- HTTPS: раскомментировать при появлении домена ---------------------
# Порядок включения TLS:
#  1. DNS A-запись домена -> IP VPS.
#  2. В .env задать DOMAIN=<домен>; в этом файле заменить
#     showtail.example на домен (3 места).
#  3. Однократно выпустить сертификат:
#     docker compose -f docker-compose.yml -f docker-compose.prod.yml \
#       run --rm certbot certonly --webroot -w /var/www/certbot \
#       -d showtail.example --email admin@showtail.example --agree-tos
#  4. Раскомментировать server-блок ниже; в :80-блоке заменить
#     location / на redirect:  return 301 https://$host$request_uri;
#  5. docker compose ... --profile tls up -d  (включает certbot-renew)
#  6. nginx перечитает сертификаты сам (reload-цикл каждые 6h в command).
#
# server {
#     listen 443 ssl;
#     http2 on;
#     server_name showtail.example;
#
#     ssl_certificate     /etc/letsencrypt/live/showtail.example/fullchain.pem;
#     ssl_certificate_key /etc/letsencrypt/live/showtail.example/privkey.pem;
#     ssl_protocols TLSv1.2 TLSv1.3;
#
#     location /auth/ {
#         limit_req zone=auth burst=10 nodelay;
#         include /etc/nginx/snippets/proxy.conf;
#         proxy_pass http://showtail_api;
#     }
#
#     location / {
#         limit_req zone=general burst=40 nodelay;
#         include /etc/nginx/snippets/proxy.conf;
#         proxy_pass http://showtail_api;
#     }
# }
```

- [ ] **Step 4: Проверить синтаксис nginx-конфига**

```powershell
docker run --rm --add-host api:127.0.0.1 -v "${PWD}\deploy\nginx\nginx.conf:/etc/nginx/nginx.conf:ro" -v "${PWD}\deploy\nginx\conf.d:/etc/nginx/conf.d:ro" -v "${PWD}\deploy\nginx\snippets:/etc/nginx/snippets:ro" nginx:1.27-alpine nginx -t
```

Ожидаемо: `syntax is ok` + `test is successful`. (`--add-host api:127.0.0.1` обязателен: nginx -t резолвит хосты upstream'ов, вне compose-сети имя `api` не существует.)

- [ ] **Step 5: Commit**

```bash
git add deploy/nginx
git commit -m "feat(deploy): nginx — edge rate-limit, websocket, заготовка TLS"
```

---

### Task 3: Backup-образ (Dockerfile + скрипты)

**Files:**
- Create: `deploy/backup/Dockerfile`
- Create: `deploy/backup/entrypoint.sh`
- Create: `deploy/backup/backup.sh`
- Create: `deploy/backup/restore.sh`

- [ ] **Step 1: Создать `deploy/backup/Dockerfile`**

```dockerfile
# Backup-контейнер ShowTail: pg_dump + mc (MinIO client) + busybox crond.
#
# База — postgres:17-alpine: pg_dump ТОЙ ЖЕ мажорной версии, что сервер.
# Несовпадение версий клиента/сервера — классическая причина дампов,
# которые «делались годами», а при restore оказались несовместимыми.
FROM postgres:17-alpine

RUN apk add --no-cache bash curl \
    && curl -fsSL https://dl.min.io/client/mc/release/linux-amd64/mc \
        -o /usr/local/bin/mc \
    && chmod +x /usr/local/bin/mc

COPY backup.sh restore.sh entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/backup.sh /usr/local/bin/restore.sh \
        /usr/local/bin/entrypoint.sh

# Переопределяем entrypoint postgres-образа (нам не нужен сервер БД,
# только клиентские утилиты + cron).
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
```

- [ ] **Step 2: Создать `deploy/backup/entrypoint.sh`**

```bash
#!/bin/sh
# Без аргументов — режим демона: crontab из $BACKUP_CRON + crond.
# С аргументами — выполняем их (ручной запуск:
#   docker compose run --rm backup backup.sh
#   docker compose run --rm backup restore.sh <dump> [db]).
set -e

if [ "$#" -gt 0 ]; then
    exec "$@"
fi

# stdout cron-задачи перенаправляем в stdout PID 1 — логи видны
# в `docker compose logs backup`.
echo "${BACKUP_CRON:-30 3 * * *} /usr/local/bin/backup.sh >> /proc/1/fd/1 2>&1" \
    > /etc/crontabs/root
echo "[backup] crond started, schedule: ${BACKUP_CRON:-30 3 * * *}"
exec crond -f -l 8
```

- [ ] **Step 3: Создать `deploy/backup/backup.sh`**

```bash
#!/bin/bash
# Бэкап ShowTail: PostgreSQL (pg_dump -Fc) + зеркало MinIO + ротация
# + опциональный offsite-S3. Расписание задаёт crond (см. entrypoint.sh),
# ручной запуск: docker compose run --rm backup backup.sh
#
# Обязательные env: POSTGRES_DB/USER/PASSWORD, S3_ENDPOINT/ACCESS_KEY/
#   SECRET_KEY/BUCKET (внутренний MinIO).
# Опциональные: BACKUP_KEEP_DAILY (7), BACKUP_KEEP_WEEKLY (4),
#   BACKUP_S3_* (все четыре заданы -> выгрузка offsite).
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/backups}"
KEEP_DAILY="${BACKUP_KEEP_DAILY:-7}"
KEEP_WEEKLY="${BACKUP_KEEP_WEEKLY:-4}"
STAMP="$(date +%F)"   # YYYY-MM-DD
DOW="$(date +%u)"     # 1=пн .. 7=вс

log() { echo "[backup $(date -Iseconds)] $*"; }

mkdir -p "$BACKUP_DIR/postgres" "$BACKUP_DIR/minio"

# --- 1. PostgreSQL: custom format (-Fc) — сжат, позволяет селективный
# restore отдельных таблиц через pg_restore -t.
DUMP="$BACKUP_DIR/postgres/showtail-$STAMP.dump"
log "pg_dump -> $DUMP"
PGPASSWORD="$POSTGRES_PASSWORD" pg_dump \
    -h postgres -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc -f "$DUMP"

# Smoke-проверка: архив читается. Молча битый дамп хуже отсутствующего —
# с ним живёшь в ложной уверенности, что бэкап есть.
pg_restore --list "$DUMP" > /dev/null
log "dump verified: $(du -h "$DUMP" | cut -f1)"

# Воскресный дамп получает weekly-копию (живёт дольше, см. ротацию).
if [ "$DOW" = "7" ]; then
    cp "$DUMP" "$BACKUP_DIR/postgres/weekly-showtail-$STAMP.dump"
    log "weekly copy created"
fi

# --- 2. Ротация: храним KEEP_DAILY свежих дневных и KEEP_WEEKLY недельных.
# ls -1t сортирует по mtime (новые сверху), tail -n +N — всё после N-го.
# Глоб showtail-* НЕ матчит weekly-showtail-* (имя должно начинаться
# с showtail-), поэтому политики не пересекаются.
ls -1t "$BACKUP_DIR/postgres/"showtail-*.dump 2>/dev/null \
    | tail -n +$((KEEP_DAILY + 1)) | xargs -r rm -f
ls -1t "$BACKUP_DIR/postgres/"weekly-showtail-*.dump 2>/dev/null \
    | tail -n +$((KEEP_WEEKLY + 1)) | xargs -r rm -f
log "rotation done (daily=$KEEP_DAILY, weekly=$KEEP_WEEKLY)"

# --- 3. MinIO: инкрементальное зеркало бакета (копируются только
# новые/изменённые объекты). --overwrite — обновлять изменившиеся.
mc alias set src "$S3_ENDPOINT" "$S3_ACCESS_KEY" "$S3_SECRET_KEY" --api S3v4
mc mirror --overwrite "src/$S3_BUCKET" "$BACKUP_DIR/minio/$S3_BUCKET"
log "minio mirrored"

# --- 4. Offsite (опционально): включается заданием всех четырёх
# BACKUP_S3_*. Не заданы — пропускаем с info-логом (осознанное решение
# спеки: сначала локально, облако добавляется четырьмя строками в .env).
if [ -n "${BACKUP_S3_ENDPOINT:-}" ] && [ -n "${BACKUP_S3_ACCESS_KEY:-}" ] \
    && [ -n "${BACKUP_S3_SECRET_KEY:-}" ] && [ -n "${BACKUP_S3_BUCKET:-}" ]; then
    mc alias set offsite "$BACKUP_S3_ENDPOINT" \
        "$BACKUP_S3_ACCESS_KEY" "$BACKUP_S3_SECRET_KEY" --api S3v4
    mc mirror --overwrite "$BACKUP_DIR" "offsite/$BACKUP_S3_BUCKET"
    log "offsite uploaded -> $BACKUP_S3_BUCKET"
else
    log "offsite skipped (BACKUP_S3_* not set)"
fi

log "backup complete"
```

- [ ] **Step 4: Создать `deploy/backup/restore.sh`**

```bash
#!/bin/bash
# Восстановление ShowTail из дампа.
#   docker compose run --rm backup restore.sh /backups/postgres/<file>.dump [dbname]
#
# По умолчанию восстанавливает в $POSTGRES_DB (боевую!). Для учения
# восстановления передавайте отдельную БД вторым аргументом:
#   docker compose exec postgres createdb -U showtail showtail_restore
#   docker compose run --rm backup restore.sh /backups/postgres/<f>.dump showtail_restore
#
# Файлы MinIO восстанавливаются зеркалом в обратную сторону:
#   docker compose run --rm backup mc alias set src http://minio:9000 <key> <secret>
#   docker compose run --rm backup mc mirror --overwrite /backups/minio/<bucket> src/<bucket>
set -euo pipefail

DUMP="${1:?usage: restore.sh <dump-file> [dbname]}"
TARGET_DB="${2:-$POSTGRES_DB}"

echo "[restore] $DUMP -> db '$TARGET_DB'"
# --clean --if-exists: дропнуть объекты перед созданием (идемпотентно);
# --no-owner: не пытаться выставлять владельца из дампа (на VPS он
# совпадает, но это делает дамп переносимым между окружениями).
PGPASSWORD="$POSTGRES_PASSWORD" pg_restore \
    --clean --if-exists --no-owner \
    -h postgres -U "$POSTGRES_USER" -d "$TARGET_DB" "$DUMP"
echo "[restore] done"
```

- [ ] **Step 5: Проверить, что образ собирается**

```powershell
docker build -t showtail-backup-test deploy/backup
```

Ожидаемо: `Successfully built`/`naming to ... done`, без ошибок apk/curl.

- [ ] **Step 6: Commit**

```bash
git add deploy/backup
git commit -m "feat(deploy): backup-контейнер — pg_dump, mc mirror, ротация, опциональный offsite"
```

---

### Task 4: docker-compose.prod.yml

**Files:**
- Create: `docker-compose.prod.yml`

- [ ] **Step 1: Создать `docker-compose.prod.yml`**

```yaml
# Прод-оверлей ShowTail. Применяется ПОВЕРХ базового файла:
#   docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
#
# Что добавляет/меняет:
# - api/worker*/migrate берут образ из ghcr (тег из IMAGE_TAG, дефолт
#   latest) — это чинит `docker compose pull` в deploy-стадии CI.
#   base `build: .` остаётся в merged-конфиге, но без --build не
#   используется; локальную приёмку гоняем с `up --build`.
# - nginx — ЕДИНСТВЕННЫЕ наружные порты (80/443). Порты инфры база
#   не публикует вовсе (см. инверсию в docker-compose.dev.yml).
# - certbot — под profile "tls", включается при появлении домена
#   (порядок включения: deploy/nginx/conf.d/showtail.conf, низ файла).
# - backup — crond: pg_dump + mc mirror + ротация (deploy/backup/).
# - Фиксированная подсеть 172.28.0.0/16: FORWARDED_ALLOW_IPS должен
#   ссылаться на стабильный CIDR, иначе proxy_headers.py не доверится
#   nginx'у после пересоздания сети.

services:
  api:
    image: ghcr.io/dmsk01/show-ring-backend:${IMAGE_TAG:-latest}
    # Прод: несколько uvicorn-воркеров вместо одного процесса.
    command: ["uvicorn", "app.main:app", "--host", "0.0.0.0",
              "--port", "8000", "--workers", "${UVICORN_WORKERS:-2}"]
    environment:
      # JSON-список для pydantic-settings (list[str]).
      FORWARDED_ALLOW_IPS: '["172.28.0.0/16"]'
    deploy:
      resources:
        limits:
          memory: 512M

  migrate:
    image: ghcr.io/dmsk01/show-ring-backend:${IMAGE_TAG:-latest}

  worker:
    image: ghcr.io/dmsk01/show-ring-backend:${IMAGE_TAG:-latest}
    deploy:
      resources:
        limits:
          memory: 512M

  worker-files:
    image: ghcr.io/dmsk01/show-ring-backend:${IMAGE_TAG:-latest}
    deploy:
      resources:
        limits:
          # Pillow (превью/watermark) — самый прожорливый воркер.
          memory: 512M

  worker-events:
    image: ghcr.io/dmsk01/show-ring-backend:${IMAGE_TAG:-latest}
    deploy:
      resources:
        limits:
          memory: 256M

  worker-email:
    image: ghcr.io/dmsk01/show-ring-backend:${IMAGE_TAG:-latest}
    deploy:
      resources:
        limits:
          memory: 256M

  worker-outbox:
    image: ghcr.io/dmsk01/show-ring-backend:${IMAGE_TAG:-latest}
    deploy:
      resources:
        limits:
          memory: 256M

  nginx:
    image: nginx:1.27-alpine
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./deploy/nginx/nginx.conf:/etc/nginx/nginx.conf:ro
      - ./deploy/nginx/conf.d:/etc/nginx/conf.d:ro
      - ./deploy/nginx/snippets:/etc/nginx/snippets:ro
      # Сертификаты и ACME-webroot шарятся с certbot.
      - certbot_etc:/etc/letsencrypt:ro
      - certbot_www:/var/www/certbot:ro
    depends_on:
      api:
        condition: service_started
    # Фоновый reload каждые 6h — подхватывать продлённые certbot'ом
    # сертификаты без ручного вмешательства. $$ — экранирование для compose.
    command: /bin/sh -c "while :; do sleep 6h & wait $${!}; nginx -s reload; done & exec nginx -g 'daemon off;'"
    restart: unless-stopped

  # Авто-продление сертификата. Выключен, пока нет домена:
  # включается `--profile tls` ПОСЛЕ первичного выпуска сертификата
  # (см. инструкцию в deploy/nginx/conf.d/showtail.conf).
  certbot:
    image: certbot/certbot
    profiles: ["tls"]
    volumes:
      - certbot_etc:/etc/letsencrypt
      - certbot_www:/var/www/certbot
    entrypoint: /bin/sh -c "trap exit TERM; while :; do certbot renew --webroot -w /var/www/certbot; sleep 12h & wait $${!}; done"
    restart: unless-stopped

  backup:
    build: ./deploy/backup
    environment:
      POSTGRES_DB: ${POSTGRES_DB:-showtail}
      POSTGRES_USER: ${POSTGRES_USER:-showtail}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-showtail}
      S3_ENDPOINT: http://minio:9000
      S3_ACCESS_KEY: ${S3_ACCESS_KEY:-showtail}
      S3_SECRET_KEY: ${S3_SECRET_KEY:-showtailminio}
      S3_BUCKET: ${S3_BUCKET:-showtail-files}
      BACKUP_CRON: ${BACKUP_CRON:-30 3 * * *}
      BACKUP_KEEP_DAILY: ${BACKUP_KEEP_DAILY:-7}
      BACKUP_KEEP_WEEKLY: ${BACKUP_KEEP_WEEKLY:-4}
      # Offsite: задать все четыре -> бэкапы зеркалятся во внешний S3.
      BACKUP_S3_ENDPOINT: ${BACKUP_S3_ENDPOINT:-}
      BACKUP_S3_ACCESS_KEY: ${BACKUP_S3_ACCESS_KEY:-}
      BACKUP_S3_SECRET_KEY: ${BACKUP_S3_SECRET_KEY:-}
      BACKUP_S3_BUCKET: ${BACKUP_S3_BUCKET:-}
    volumes:
      # bind-mount: бэкапы видны с хоста как ./backups без docker-команд.
      - ./backups:/backups
    depends_on:
      postgres:
        condition: service_healthy
      minio:
        condition: service_healthy
    restart: unless-stopped

# Фиксированная подсеть default-сети — стабильный CIDR для
# FORWARDED_ALLOW_IPS (см. комментарий в шапке).
networks:
  default:
    ipam:
      config:
        - subnet: 172.28.0.0/16

volumes:
  certbot_etc:
  certbot_www:
```

- [ ] **Step 2: Добавить `backups/` в .gitignore и .dockerignore**

В `.gitignore` и `.dockerignore` добавить строку:

```
backups/
```

- [ ] **Step 3: Проверить валидность прод-связки**

```powershell
docker compose -f docker-compose.yml -f docker-compose.prod.yml config --quiet
```

Ожидаемо: молчание (exit 0). Затем убедиться, что наружу торчат только 80/443:

```powershell
docker compose -f docker-compose.yml -f docker-compose.prod.yml config --format json | python -c "import json,sys; svc=json.load(sys.stdin)['services']; print({k: [p.get('published') for p in v.get('ports', [])] for k,v in svc.items() if v.get('ports')})"
```

Ожидаемо: `{'nginx': ['80', '443']}` — и больше ни одного сервиса с портами.

- [ ] **Step 4: Commit**

```bash
git add docker-compose.prod.yml .gitignore .dockerignore
git commit -m "feat(deploy): прод-оверлей compose — nginx, certbot (profile tls), backup, образы из ghcr"
```

---

### Task 5: .env.prod.example

**Files:**
- Create: `.env.prod.example`

- [ ] **Step 1: Создать `.env.prod.example`**

```bash
# ============================================================
# ShowTail — прод-переменные (.env на VPS, chmod 600, ВНЕ git)
# Скопировать: cp .env.prod.example .env  и заполнить значения.
# ============================================================

# --- Обязательные секреты ---
# >=32 символов, НЕ плейсхолдер: app/config.py валидирует на старте
# и при debug=false роняет приложение. Сгенерировать:
#   openssl rand -hex 32
SECRET_KEY=

POSTGRES_DB=showtail
POSTGRES_USER=showtail
POSTGRES_PASSWORD=

RABBITMQ_USER=showtail
RABBITMQ_PASSWORD=

# MinIO (внутренний S3 для файлов пользователей)
S3_ACCESS_KEY=
S3_SECRET_KEY=
S3_BUCKET=showtail-files

# --- Прод-режим приложения ---
DEBUG=false
SCHEDULER_ENABLED=true
# Реальный SMTP (Sendgrid/Mailgun/SES), НЕ mailpit
SMTP_HOST=
SMTP_PORT=587
SMTP_USE_TLS=true
SMTP_FROM_EMAIL=noreply@showtail.example

# --- Деплой ---
# Тег образа из ghcr (CI пушит sha-теги и latest)
IMAGE_TAG=latest
UVICORN_WORKERS=2

# --- TLS (заполнить при появлении домена) ---
# DOMAIN=showtail.example

# --- Бэкапы ---
# BACKUP_CRON=30 3 * * *
# BACKUP_KEEP_DAILY=7
# BACKUP_KEEP_WEEKLY=4
# Offsite-S3: задать все четыре -> бэкапы уезжают во внешнее облако.
# Не заданы -> только локальный ./backups (см. спеку: осознанный старт).
# BACKUP_S3_ENDPOINT=https://s3.storage.selcloud.ru
# BACKUP_S3_ACCESS_KEY=
# BACKUP_S3_SECRET_KEY=
# BACKUP_S3_BUCKET=showtail-backups
```

- [ ] **Step 2: Commit**

```bash
git add .env.prod.example
git commit -m "docs(deploy): .env.prod.example — перечень прод-переменных"
```

---

### Task 6: Обновить deploy-стадии CI (оба файла)

**Files:**
- Modify: `.github/workflows/ci.yml` (job deploy, script)
- Modify: `.gitlab-ci.yml` (job deploy, script)

- [ ] **Step 1: GitHub Actions — прод-связка compose в deploy**

В `.github/workflows/ci.yml` заменить script деплой-шага на:

```yaml
          script: |
            set -e
            cd /opt/showtail
            git pull --ff-only
            echo "${{ secrets.REGISTRY_TOKEN }}" | docker login ghcr.io -u "${{ secrets.REGISTRY_USER }}" --password-stdin
            docker compose -f docker-compose.yml -f docker-compose.prod.yml pull
            docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm migrate
            docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build backup
            docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
            docker image prune -f
```

И обновить комментарий над шагом: compose-файлы приезжают на сервер через `git pull` (репозиторий клонирован в /opt/showtail), `--build backup` пересобирает локальный backup-образ при изменении скриптов.

- [ ] **Step 2: GitLab CI — то же самое**

В `.gitlab-ci.yml` заменить script деплой-джоба на:

```yaml
  script:
    - ssh "$DEPLOY_USER@$DEPLOY_HOST" "cd /opt/showtail
        && git pull --ff-only
        && docker compose -f docker-compose.yml -f docker-compose.prod.yml pull
        && docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm migrate
        && docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build backup
        && docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
        && docker image prune -f"
```

- [ ] **Step 3: Проверить YAML-валидность обоих файлов**

```powershell
python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml', encoding='utf-8')); yaml.safe_load(open('.gitlab-ci.yml', encoding='utf-8')); print('ok')"
```

Ожидаемо: `ok`. (PyYAML есть в venv как транзитивная зависимость; если нет — `pip install pyyaml`.)

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/ci.yml .gitlab-ci.yml
git commit -m "ci: deploy через прод-связку compose (-f prod), git pull на сервере"
```

---

### Task 7: docs/knowledge/backups.md

**Files:**
- Create: `docs/knowledge/backups.md`

- [ ] **Step 1: Написать knowledge-статью**

Формат — как остальные файлы `docs/knowledge/` (Markdown, русский). Содержание (полный текст пишется при выполнении, структура обязательная):

1. **Зачем**: бэкап на том же диске ≠ бэкап; RPO суточного дампа; что бэкапим (PG + MinIO) и почему НЕ бэкапим Redis/RabbitMQ.
2. **pg_dump форматы**: plain vs custom (-Fc) — почему custom (сжатие, селективный restore, pg_restore --list как verify).
3. **Ротация**: 7 дневных + 4 недельных, как работает `ls -1t | tail -n +N`.
4. **Offsite**: правило 3-2-1, как включить BACKUP_S3_* (4 строки в .env).
5. **Restore-процедура**: пошагово restore.sh в отдельную БД и в боевую; mc mirror обратно для файлов; восстановление-учение раз в квартал.
6. **Ограничения и апгрейд-путь**: суточный RPO; когда переходить на WAL-архивацию (pgBackRest/wal-g) — рост до «потеря дня данных недопустима».

- [ ] **Step 2: Commit**

```bash
git add docs/knowledge/backups.md
git commit -m "docs(knowledge): бэкапы — pg_dump, ротация, restore-учения, правило 3-2-1"
```

---

### Task 8: Локальная приёмка (по спеке, раздел 5)

Прод-связка локально. Требуется заполненный `.env` (локальный dev `.env` подходит — SECRET_KEY уже валидный).

- [ ] **Step 1: Поднять прод-связку с локальной сборкой**

```powershell
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
```

Ожидаемо: nginx, api, postgres, redis, rabbitmq, minio, worker, worker-files, backup — Up/healthy; migrate, minio-init — Exited (0).

- [ ] **Step 2: API через nginx, прямые порты закрыты**

```powershell
curl.exe -s -o NUL -w "%{http_code}" http://localhost/health        # ожидаемо 200
curl.exe -s -o NUL -w "%{http_code}" --max-time 3 http://localhost:8000/health  # ожидаемо 000 (connection refused/timeout)
Test-NetConnection localhost -Port 5432 | Select-Object TcpTestSucceeded        # ожидаемо False*
```

*Если локальный PostgreSQL 18-сервис Windows когда-то включён обратно — порт 5432 может отвечать им, не Docker'ом; проверять `docker compose ... ps`, а не только порт.

- [ ] **Step 3: Edge rate-limit — флуд на /auth/login**

```powershell
1..30 | ForEach-Object { curl.exe -s -o NUL -w "%{http_code} " -X POST http://localhost/auth/login }
```

Ожидаемо: первые ~10-15 ответов — 422 (FastAPI: нет тела запроса), дальше — 429 от nginx. Признак, что 429 именно от nginx: тело ответа — HTML-страница nginx, не JSON `{"detail": ...}`.

- [ ] **Step 4: WebSocket через nginx**

```powershell
python -c "import asyncio, websockets; asyncio.run(websockets.connect('ws://localhost/ws/notifications').__aenter__()) and None; print('ws connected')"
```

Ожидаемо: соединение устанавливается (или закрывается приложением с кодом аутентификации — главное, НЕ HTTP 400/502 от nginx: это значило бы, что Upgrade не проброшен).

- [ ] **Step 5: Бэкап вручную + проверка дампа**

```powershell
docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm backup backup.sh
Get-ChildItem backups\postgres
```

Ожидаемо: лог `dump verified`, `minio mirrored`, `offsite skipped (BACKUP_S3_* not set)`, `backup complete`; в `backups/postgres/` файл `showtail-<сегодня>.dump` ненулевого размера; в `backups/minio/` — содержимое бакета.

- [ ] **Step 6: Restore-учение в отдельную БД**

```powershell
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec postgres createdb -U showtail showtail_restore
docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm backup restore.sh /backups/postgres/showtail-$(Get-Date -Format yyyy-MM-dd).dump showtail_restore
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec postgres psql -U showtail -d showtail_restore -c "SELECT count(*) FROM users;"
```

Ожидаемо: pg_restore без ошибок; count(*) совпадает с боевой БД (`psql -d showtail -c "SELECT count(*) FROM users;"`). После учения: `dropdb -U showtail showtail_restore`.

- [ ] **Step 7: Ротация**

```powershell
1..10 | ForEach-Object { New-Item -ItemType File "backups\postgres\showtail-2026-01-0$($_ % 10).dump" } 
docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm backup backup.sh
(Get-ChildItem backups\postgres\showtail-*.dump).Count
```

Ожидаемо: ровно 7 файлов (свежий настоящий + 6 новейших пустышек, остальные удалены).

- [ ] **Step 8: Снести связку и убедиться, что dev живёт как раньше**

```powershell
docker compose -f docker-compose.yml -f docker-compose.prod.yml down
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d
curl.exe -s -o NUL -w "%{http_code}" http://localhost:8000/health   # ожидаемо 200 (прямой порт из dev-override)
```

- [ ] **Step 9: Финальный коммит (фиксы по итогам приёмки, если были)**

```bash
git add -A deploy docker-compose.prod.yml
git commit -m "test(deploy): локальная приёмка прод-связки — фиксы по результатам"
```

(Если фиксов не было — шаг пропустить.)

---

## Self-review (выполнен при написании плана)

- **Покрытие спеки:** бэкапы → Task 3+4+8; nginx/TLS → Task 2+4; инверсия портов → Task 1; prod-compose/registry → Task 4; секреты → Task 5; CI → Task 6; knowledge-док → Task 7; приёмка 7 пунктов спеки → Task 8 (пункты 1-6) + Task 6 Step 3 (пункт 7).
- **Типы/имена сквозные:** образ `ghcr.io/dmsk01/show-ring-backend` совпадает с CI (`ghcr.io/${{ github.repository }}`); подсеть `172.28.0.0/16` одинаковая в compose и FORWARDED_ALLOW_IPS; пути `/backups`, `/var/www/certbot`, `/etc/letsencrypt` согласованы между nginx/certbot/backup.
- **Известные компромиссы:** merged-конфиг прода сохраняет `build: .` у api (compose не умеет удалять ключи) — безвредно, pull использует image; nginx reload-цикл 6h вместо deploy-hook certbot — проще и достаточно.
