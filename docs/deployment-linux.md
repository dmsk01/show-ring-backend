# Развёртывание ShowTail на Linux-сервере

Полная инструкция: от клонирования репозитория до засеянной БД и работающего
прод-стека за nginx с TLS.

Этот гайд описывает **полный прод-стек** (бэкенд + воркеры + инфраструктура +
nginx + TLS + бэкапы) со **сборкой образов бэкенда прямо на сервере**
(`up --build`), без обязательного доступа к приватному registry для бэка.

> Локальная разработка под Windows — отдельная инструкция в корневом
> [`README.md`](../README.md). Здесь — только сервер.

---

## 0. Что должно быть на сервере

- **ОС:** любой современный Linux (Ubuntu 22.04/24.04 LTS — эталон).
- **Docker Engine + Docker Compose v2** (плагин `docker compose`, не старый
  `docker-compose`). Проверка:
  ```bash
  docker --version
  docker compose version
  ```
  Если не установлено — официальный способ:
  ```bash
  curl -fsSL https://get.docker.com | sh
  sudo usermod -aG docker $USER   # чтобы не писать sudo перед docker; перелогиниться
  ```
- **git**: `sudo apt-get install -y git`.
- **Ресурсы:** ~2 ГБ свободной RAM под стек (Pillow-воркер и сборка образа —
  самые прожорливые). Под сборку нужно ещё ~1–2 ГБ временно.
- **Сетевые порты:** наружу открыты только **80** и **443** (их публикует
  nginx). Порты PostgreSQL/Redis/RabbitMQ/MinIO в проде **не публикуются**
  вовсе — это заложено в `docker-compose.yml` (host-порты добавляет только
  dev-оверлей). В фаерволе (ufw/security group облака) откройте 80/443 и 22 (SSH).

---

## 1. Клонировать репозиторий

```bash
cd /opt                       # или любой каталог под сервисы
git clone git@github.com:dmsk01/show-ring-backend.git showtail
cd showtail
```

> Если на сервере нет SSH-ключа для GitHub — клонируйте по HTTPS:
> `git clone https://github.com/dmsk01/show-ring-backend.git showtail`.

---

## 2. Создать `.env` и заполнить секреты

В репозитории `.env` **не лежит** (он в `.gitignore`) — его создаём из шаблона:

```bash
cp .env.prod.example .env
chmod 600 .env                # секреты не должны читаться кем попало
```

Сгенерируйте значения и впишите их в `.env` (любым редактором, например `nano .env`):

```bash
openssl rand -hex 32          # → SECRET_KEY (минимум 32 символа, ОБЯЗАТЕЛЬНО)
openssl rand -hex 24          # → POSTGRES_PASSWORD
openssl rand -hex 24          # → RABBITMQ_PASSWORD
openssl rand -hex 16          # → S3_ACCESS_KEY
openssl rand -hex 24          # → S3_SECRET_KEY
```

**Обязательно заполнить:**

| Переменная | Чем заполнить |
|---|---|
| `SECRET_KEY` | `openssl rand -hex 32`. При `DEBUG=false` приложение **упадёт на старте**, если ключ короче 32 символов или похож на плейсхолдер (валидация в `app/config.py`). |
| `POSTGRES_PASSWORD` | случайная строка |
| `RABBITMQ_PASSWORD` | случайная строка |
| `S3_ACCESS_KEY` / `S3_SECRET_KEY` | случайные строки (доступ к внутреннему MinIO) |
| `DEBUG` | `false` (уже стоит в шаблоне — не менять) |
| `SCHEDULER_ENABLED` | `true` (фоновые задачи: дедлайны выставок и т.п.) |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_FROM_EMAIL` | реквизиты **реального** SMTP (Sendgrid/Mailgun/SES). В проде нет mailpit — без рабочего SMTP не уйдут письма верификации и уведомлений. |

`POSTGRES_DB`/`POSTGRES_USER` можно оставить `showtail`. `COOKIE_PATH_PREFIX=/api`
уже задан — без него браузер не приложит auth-куки к `/api/*` и логин «молча» не
сработает. Не трогайте его.

> **Почему столько переменных прокидывается явно?** `.env` не попадает в образ
> (он в `.dockerignore`), поэтому всё, что нужно `app.config.Settings`,
> compose передаёт в контейнеры через `environment:`. Менять переменные → править
> `.env` и перезапускать сервис.

---

## 3. Клонировать репозиторий фронтенда рядом

Прод-стек включает сервис `web` (Next.js, отдельный репозиторий
`show-ring-frontend`). Он собирается **локально** из соседнего каталога —
в `docker-compose.prod.yml` указано `build: ../show-ring-frontend`. Никакой
внешний registry не нужен: фронт, как и бэкенд, билдится на сервере.

Поэтому репо фронта должен лежать **рядом** с этим репо (на один уровень выше):

```bash
cd ..        # подняться из каталога бэкенда (showtail)
git clone git@github.com:dmsk01/show-ring-frontend.git
cd showtail  # вернуться в бэкенд
```

Раскладка на сервере должна получиться такой:

```
/opt/
├── showtail/              ← этот репо (бэкенд), отсюда запускаем compose
└── show-ring-frontend/    ← репо фронта, его собирает сервис web
```

> Если каталог фронта называется иначе или лежит в другом месте — поправьте путь
> `build: ../show-ring-frontend` в `docker-compose.prod.yml` под свою раскладку.
> nginx стартует только когда `web` стал healthy, поэтому без собранного фронта
> весь стек до конца не поднимется.

---

## 4. Собрать образы и поднять стек

Бэкенд (api, воркеры, migrate, backup) собирается прямо на сервере флагом
`--build`. Прод-оверлей применяется **поверх** базового compose:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml --env-file .env up -d --build
```

Что произойдёт автоматически:

1. Соберутся образы бэкенда из `Dockerfile` и фронта из `../show-ring-frontend`
   (multi-stage, первый билд — несколько минут; фронт ставит npm-зависимости).
2. Поднимутся PostgreSQL, RabbitMQ, Redis, MinIO (порты наружу не публикуются).
3. Одноразовый `minio-init` создаст bucket для файлов.
4. Одноразовый `migrate` накатит миграции (`alembic upgrade head`) **до** старта api.
5. Стартуют `api` (несколько uvicorn-воркеров), `worker`, `worker-files`, `web`, `nginx`.

> **Email-воркеры** (`worker-events`, `worker-email`, `worker-outbox`) сидят под
> профилем `events` и по умолчанию **не стартуют**. Без `worker-outbox`
> транзакционный outbox не доставляет письма — события копятся в БД. Чтобы
> поднять весь email-пайплайн, добавьте профиль:
> ```bash
> docker compose -f docker-compose.yml -f docker-compose.prod.yml --env-file .env --profile events up -d
> ```

Проверьте, что всё живо:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
```

Все сервисы должны быть `running`/`healthy`; `migrate` и `minio-init` —
`exited (0)` (это одноразовые контейнеры, так и должно быть).

На этом этапе API уже отвечает **внутри сети**. Снаружи он доступен через nginx
на `http://<IP-сервера>/` (фронт) и `http://<IP-сервера>/api/` (API). Health:

```bash
curl -fsS http://localhost/api/health
```

---

## 5. Подключить домен и TLS (HTTPS)

Пока домена нет, стек работает по HTTP на `:80`. Для боевого режима нужен домен и
сертификат Let's Encrypt (порядок продублирован в
`deploy/nginx/conf.d/showtail.conf`, низ файла):

1. **DNS:** заведите A-запись домена → IP сервера. Дождитесь, пока резолвится:
   `dig +short showtail.example`.
2. В `.env` задайте `DOMAIN=showtail.example`.
3. В `deploy/nginx/conf.d/showtail.conf` замените `showtail.example` на ваш домен
   (3 места в закомментированном 443-блоке).
4. Однократно выпустите сертификат (webroot-челлендж ходит через уже работающий
   nginx на :80):
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm certbot \
     certonly --webroot -w /var/www/certbot \
     -d showtail.example --email admin@showtail.example --agree-tos
   ```
5. Раскомментируйте `server { listen 443 ssl; ... }` в `showtail.conf`, а в
   `:80`-блоке замените `location /` на редирект:
   `return 301 https://$host$request_uri;`.
6. Перезапустите с включённым авто-продлением сертификата:
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.prod.yml --env-file .env --profile tls up -d
   ```
   nginx сам перечитывает сертификаты (reload-цикл каждые 6 часов заложен в его
   команде), а сервис `certbot` продлевает их каждые 12 часов.

---

## 6. Создать первого администратора

После миграций в БД нет ни одного admin'а (регистрация через `/auth/register`
создаёт обычного пользователя). Создаём админа идемпотентным скриптом внутри
контейнера `api`:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec api \
  python -m scripts.bootstrap_admin --email admin@showtail.example --password 'СильныйПароль123!'
```

Скрипт идемпотентный — повторный запуск безопасен.

---

## 7. Засеять базу

Скрипты запускаются внутри контейнера `api` — у него уже есть все нужные
переменные окружения (`DATABASE_URL`, доступ к MinIO). Дальше — `docker compose
… exec api …`; команды ниже укорочены до `exec api` (полный префикс с `-f` файлами
тот же, что выше).

### 7.1. Справочники — ОБЯЗАТЕЛЬНО

Без справочников (виды животных, FCI-группы, породы, выставочные классы, титулы,
оценки) приложение нефункционально — на них завязаны регистрации и результаты.
Скрипт идемпотентный:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec api \
  python -m scripts.seed_references
```

### 7.2. Демо-данные — ОПЦИОНАЛЬНО (только для тестового/демо-стенда)

Наполняет БД разнообразными данными по всем разделам (питомники, собаки,
помёты, выставки, объявления, блог, тикеты и т.д.) — удобно «пощупать» UI.
**На боевом стенде с реальными пользователями обычно НЕ запускают** — это
синтетика. Идемпотентный, использует отдельные namespace'ы (`*-demo@dogshow.ru`),
чтобы не конфликтовать с реальными данными:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec api \
  python -m scripts.seed_demo
```

### 7.3. E2E-пользователи — ОПЦИОНАЛЬНО (под Playwright-тесты фронта)

Только если на этом стенде гоняются e2e-тесты фронтенда. Пароль у всех —
`Password123!`, аккаунты сразу активны:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec api \
  python -m scripts.seed_e2e_users --force
```

---

## 8. Бэкапы

Сервис `backup` (crond) уже включён прод-оверлеем: ежедневно делает `pg_dump` +
зеркалит файлы из MinIO и ротирует старые копии. Бэкапы видны на хосте в
`./backups` (bind-mount, без docker-команд). Параметры — в `.env`:

```ini
BACKUP_CRON=30 3 * * *        # когда (по умолчанию 03:30)
BACKUP_KEEP_DAILY=7           # сколько дневных копий хранить
BACKUP_KEEP_WEEKLY=4          # сколько недельных
```

Для **offsite-копий** во внешний S3 задайте все четыре переменные
`BACKUP_S3_*` (endpoint/ключи/bucket) — иначе бэкапы только локальные.

Разовый бэкап вручную:
```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec backup /backup.sh
```

---

## 9. Обновление кода

После `git pull` нового кода бэкенда — **обязательно** пересобирайте образы,
иначе Docker оставит старые слои (частая причина рассинхрона кода и миграций,
`alembic … Can't locate revision …`):

```bash
git pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml --env-file .env up -d --build
```

Миграции накатятся автоматически контейнером `migrate` при каждом `up`.

---

## 10. Эксплуатация и неполадки

```bash
# короткий алиас, чтобы не повторять -f каждый раз (добавьте в ~/.bashrc):
alias dc='docker compose -f docker-compose.yml -f docker-compose.prod.yml --env-file .env'

dc ps                          # статусы всех сервисов
dc logs -f api                 # логи API в реальном времени
dc logs --tail=50 migrate      # почему упала миграция
dc restart worker worker-files # перезапустить воркеры после правок их кода
dc down                        # остановить всё (данные в volume сохраняются)
dc down -v                     # ОСТАНОВИТЬ и СТЕРЕТЬ ВСЕ ДАННЫЕ (чистый старт)
```

- **Стек сам поднимается после ребута сервера** — у сервисов `restart:
  unless-stopped`.
- **`/api/health` отдаёт 503 или таймаут** — обычно отвалилась зависимость.
  Смотрите `dc ps`: если сервис в `Exited` — `dc up -d`, затем `dc logs <сервис>`.
- **api/migrate падает с ошибкой про SECRET_KEY** — ключ короче 32 символов или
  выглядит как плейсхолдер при `DEBUG=false`. Сгенерируйте `openssl rand -hex 32`.
- **nginx не стартует, ждёт `web`** — фронт не собрался или unhealthy. Проверьте,
  что репо `show-ring-frontend` лежит рядом (шаг 3) и путь `build:` в
  `docker-compose.prod.yml` верный; смотрите `dc logs web`.
- **Логин не логинит, кук нет** — проверьте `COOKIE_PATH_PREFIX=/api` в `.env`.

---

## Краткая шпаргалка (TL;DR)

```bash
# 1. клон бэкенда + фронта рядом
git clone git@github.com:dmsk01/show-ring-backend.git showtail
git clone git@github.com:dmsk01/show-ring-frontend.git
cd showtail

# 2. секреты
cp .env.prod.example .env && chmod 600 .env
# заполнить SECRET_KEY (openssl rand -hex 32), пароли PG/Rabbit/MinIO, SMTP

# 3. сборка и старт (бэк и фронт собираются локально, registry не нужен)
docker compose -f docker-compose.yml -f docker-compose.prod.yml --env-file .env up -d --build

# 4. админ + справочники (обязательно)
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec api python -m scripts.bootstrap_admin --email admin@showtail.example --password '...'
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec api python -m scripts.seed_references

# 5. проверка
curl -fsS http://localhost/api/health
```
