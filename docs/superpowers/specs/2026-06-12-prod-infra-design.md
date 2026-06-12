# Прод-инфраструктура ShowTail: бэкапы, nginx/TLS, prod-compose, секреты

**Дата:** 2026-06-12
**Статус:** дизайн утверждён пользователем (brainstorming-сессия)

## Контекст и цель

Аудит показал: CI/CD добавлен (`.github/workflows/ci.yml`, `.gitlab-ci.yml`),
но прод-слой отсутствует — нет reverse-proxy/TLS, нет бэкапов, deploy-стадия
CI ссылается на образы из registry, которых compose не использует
(`build: .` везде), порты инфры (PG/Redis/Rabbit/MinIO) открыты наружу.

Цель: довести проект до состояния «можно выкатить на один VPS и не потерять
данные». Метрики/observability — осознанно вне скоупа (решение пользователя).

## Утверждённые решения

| Вопрос | Решение |
|---|---|
| Хостинг | Один VPS с Docker Compose |
| Offsite-бэкапы | Сначала локально на VPS; выгрузка в S3 включается env-переменными без правки кода |
| Состав бэкапов | PostgreSQL (pg_dump) + файлы MinIO (mc mirror). Redis/RabbitMQ не бэкапим — транзитные |
| Механизм бэкапов | Вариант A: отдельный backup-сервис в compose со своим cron (версионируется в git) |
| Reverse-proxy | nginx + certbot (учебная ценность, edge rate-limit; Caddy отклонён) |
| Домен | Пока нет. Конфиг 443 заготовлен, включается при появлении домена; certbot дальше продлевает сертификат автоматически |
| Секреты | `.env` на сервере, chmod 600; в git — `.env.prod.example`. Vault — YAGNI для одного VPS |

## Структура файлов

```
docker-compose.prod.yml        # прод-оверлей (образы из registry, nginx, certbot, backup)
deploy/
  nginx/
    nginx.conf                 # limit_req-зоны, client_max_body_size, websocket map
    conf.d/showtail.conf       # server-блок :80 (+ :443, закомментирован до домена)
  backup/
    Dockerfile                 # postgres:17-alpine + mc + busybox crond
    backup.sh                  # дамп + зеркало + ротация + опциональный S3-upload
    restore.sh                 # процедура восстановления
.env.prod.example              # перечень прод-переменных (без значений)
docs/knowledge/backups.md      # концепция: форматы pg_dump, ротация, restore-учения
```

Изменяются существующие файлы:

- `docker-compose.yml` — порты инфры переезжают в dev-override (см. ниже)
- `docker-compose.dev.yml` — принимает эти порты
- `.github/workflows/ci.yml` и `.gitlab-ci.yml` — deploy-скрипт запускает
  compose с `-f docker-compose.yml -f docker-compose.prod.yml`

## 1. Бэкапы — сервис `backup`

**Образ:** база `postgres:17-alpine` — `pg_dump` той же мажорной версии, что
сервер (несовпадение версий = классическая причина битых дампов). Добавляем
статический бинарь `mc` (MinIO client). Планировщик — busybox crond,
расписание из env `BACKUP_CRON` (дефолт `30 3 * * *`).

**PostgreSQL:** `pg_dump -Fc` (custom format: сжат, позволяет селективный
restore отдельных таблиц) в `/backups/postgres/showtail-YYYY-MM-DD.dump`.
Сразу после дампа — `pg_restore --list` как smoke-проверка читаемости архива;
нечитаемый дамп → выход с ошибкой и громкий лог.

**MinIO:** `mc mirror` бакета в `/backups/minio/` — инкрементально,
копируются только новые/изменённые объекты.

**Ротация:** 7 дневных + 4 недельных (воскресный дамп живёт месяц).
Параметры `BACKUP_KEEP_DAILY` / `BACKUP_KEEP_WEEKLY`. Ротация применяется
только к дампам PG; зеркало MinIO — живая копия, не версионируется.

**Offsite (опциональный):** если заданы все четыре переменные
`BACKUP_S3_ENDPOINT` / `BACKUP_S3_ACCESS_KEY` / `BACKUP_S3_SECRET_KEY` /
`BACKUP_S3_BUCKET` — после локального бэкапа `mc mirror` каталога `/backups`
во внешний S3-бакет. Не заданы — шаг пропускается с info-логом. Появится
облако — четыре строки в `.env`, без правки файлов.

**Volume:** `./backups` (bind-mount на хост) — бэкапы видны и доступны
с хоста без docker-команд.

**restore.sh:** восстановление через `pg_restore --clean --if-exists` в
указанную БД + `mc mirror` в обратную сторону для файлов. Прогоняется
локально как часть приёмки — непроверенный бэкап бэкапом не считается.

## 2. nginx + certbot

**Роль:** единственные наружные порты VPS — 80/443. PG, Redis, Rabbit,
MinIO, api:8000 — только внутренняя сеть compose.

**Проксирование:** `proxy_pass http://api:8000`, websocket upgrade
(map `$http_upgrade`) — без него live-уведомления (этап 16) отвалятся.
Заголовки `X-Forwarded-For` / `X-Forwarded-Proto` проставляет nginx.

**Edge rate-limit (`limit_req`):**

- общая зона ~20 r/s, burst 40 — на всё;
- жёсткая зона ~5 r/s на `/auth/` (login/register/OTP).

nginx отдаёт 429 до того, как запрос дойдёт до Python. Application-level
`progressive_ban.py` остаётся второй линией (умнее: экспоненциальные баны,
fail_closed на auth) — defence-in-depth, а не замена.

**Лимит тела:** `client_max_body_size 10m` — зеркалит
`max_upload_size_bytes` приложения.

**TLS-сценарий:**

- Сейчас (без домена): работает только server-блок `:80`; 443-блок и
  certbot-сервис закомментированы/выключены.
- При появлении домена: задать `DOMAIN` в `.env`, включить 443-блок,
  однократно выпустить сертификат (`certbot certonly --webroot`),
  дальше certbot-контейнер в цикле `renew` каждые 12 часов продлевает
  сертификат сам; nginx перечитывает сертификаты по reload.

**Доверие прокси:** в compose фиксируется подсеть сети (`172.28.0.0/16`
через ipam), у api ставится `FORWARDED_ALLOW_IPS=["172.28.0.0/16"]` —
иначе `proxy_headers.py` не доверится nginx и rate-limit будет банить IP
прокси вместо клиентов.

## 3. prod-compose и перенос портов

**Ключевое ограничение compose:** override не умеет удалять ключи — списки
`ports` мёржатся. Убрать открытый порт прод-оверлеем нельзя. Поэтому
инвертируем:

- из базового `docker-compose.yml` уходят все `ports:` инфры
  (5432, 6379, 5672, 15672, 9000, 9001) и `8000` у api;
- `docker-compose.dev.yml` добавляет их обратно для dev-удобства
  (DBeaver, Rabbit UI, MinIO console, прямой api:8000);
- база становится прод-безопасной по умолчанию.

Поведенческое следствие (пользователь предупреждён и согласен): «голый»
`docker compose up` больше не публикует порты инфры — для dev обязательны
оба `-f`, как уже описано в шапке compose-файла.

**`docker-compose.prod.yml`:**

- `api` / `worker*` / `migrate`:
  `image: ghcr.io/dmsk01/show-ring-backend:${IMAGE_TAG:-latest}` вместо
  `build: .` — чинит `docker compose pull` в deploy-стадии CI;
- сервисы `nginx` (ports 80/443), `certbot` (выключен до домена), `backup`;
- api: `--workers ${UVICORN_WORKERS:-2}`, без `--reload`;
- `deploy.resources.limits.memory` на api/worker'ы;
- фиксированная подсеть сети (ipam).

Запуск прода:
`docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d`.
Deploy-стадии обоих CI обновляются под эту команду.

## 4. Секреты

`.env` на сервере (chmod 600, вне git). В git — `.env.prod.example`:
перечень всех обязательных переменных с комментариями (SECRET_KEY ≥32
символов — валидатор `app/config.py` падает на старте при плейсхолдере,
POSTGRES_PASSWORD, S3-ключи, BACKUP_S3_* как опциональный блок, DOMAIN).
Vault/secrets-manager отклонён: на одном VPS добавляет инфраструктуру,
которая сама требует секретов для доступа к секретам.

## 5. Приёмка

1. Локально поднять прод-связку (nginx без TLS): API отвечает через `:80`;
   `:8000` и порты инфры снаружи недоступны.
2. WebSocket уведомлений работает через nginx.
3. Флуд на `/auth/login` → nginx отдаёт 429 (до приложения).
4. `backup.sh` вручную: дамп + зеркало созданы, `pg_restore --list` чистый.
5. Restore-учение: `restore.sh` в пустую БД → данные на месте.
6. Ротация: файлы с датами в прошлом удаляются по правилам keep-политики.
7. CI: lint+test зелёные; deploy-скрипт ссылается на оба compose-файла.

## Вне скоупа

- Метрики/Prometheus/Sentry (решение пользователя — «пока не нужны»).
- Zero-downtime деплой и expand/contract миграции.
- Контейнеризация фронтенда.
- Staging-окружение.
- WAL-архивация / point-in-time recovery (достаточно суточных дампов
  на текущем масштабе; апгрейд-путь — pgBackRest/wal-g, не ломает схему).
