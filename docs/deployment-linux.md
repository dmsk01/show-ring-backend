# Развёртывание ShowTail на Linux-сервере

Полная инструкция: от клонирования репозитория до засеянной БД и работающего
прод-стека за nginx с TLS.

Этот гайд описывает **полный прод-стек** (бэкенд + воркеры + инфраструктура +
nginx + TLS + бэкапы) со **сборкой образов бэкенда прямо на сервере**
(`up --build`), без обязательного доступа к приватному registry для бэка.

Гайд покрывает два сценария:
- **Боевой сервер** (VPS, свой домен, HTTPS) — разделы 0–10 ниже.
- **Локальный запуск на своей Linux-машине** (прогнать весь стек целиком,
  без домена, по `http://localhost`) — см. [Приложение: локальное
  развёртывание](#приложение-локальное-развёртывание-на-этой-же-машине).
  Топология та же (web + nginx + воркеры), отличается лишь конфигом.

> Локальная разработка под Windows с hot-reload и mailpit — отдельная
> инструкция в корневом [`README.md`](../README.md).

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

### Фронтенд: что нужно и чего НЕ нужно

У фронта **нет отдельной процедуры развёртывания** — в этой схеме он
разворачивается как часть бэкенд-стека. Достаточно одного действия выше
(склонировать репо рядом). Конкретно:

- ✅ **Клонировать `show-ring-frontend` рядом** — единственный ручной шаг.
- ✅ **Сборка** — автоматически, частью `docker compose up --build` (сервис `web`,
  его `Dockerfile`: Next.js standalone, non-root, порт 8082, healthcheck `/healthz`).
- ❌ **Свой `.env` фронту задавать НЕ нужно.** Образ собирается с дефолтами
  `NEXT_PUBLIC_SERVER_URL=/api` (в его `Dockerfile` помечено: секреты на билде не
  нужны), а `BACKEND_URL=http://api:8000` мы задаём в `web.environment`
  прод-оверлея — для SSR-запросов изнутри контейнера.
- ❌ **Отдельно `npm install` / `npm run build` на сервере запускать НЕ нужно** —
  всё происходит внутри Docker-сборки образа.
- ✅ **Маршрутизация** — через nginx: `/` → `web`, `/api/` → `api`
  (`deploy/nginx/conf.d/showtail.conf`).

> **Расхождение с README фронта.** В `show-ring-frontend/README.md` раздел
> «Деплой» описывает CI-путь (сборка → публикация образа в ghcr → деплой на VPS).
> Здесь мы намеренно используем **локальную сборку** на сервере вместо ghcr —
> ради нулевой зависимости от внешнего реестра. Команды из README фронта
> (`npm run dev` и т.п.) актуальны только для разработки самого фронта.

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

---

## Приложение: локальное развёртывание на этой же машине

Запуск всего прод-стека прямо на твоём ноутбуке с Ubuntu 24 (например, чтобы
прогнать связку web + nginx + бэкенд целиком перед выкаткой на боевой сервер).
Это **тот же стек**, что и выше, с тремя поправками: без домена/TLS, доступ по
`http://localhost`, и `DEBUG=true`, чтобы работал логин по HTTP (см. ниже).

Выполняй разделы **1–4** и **6–7** как для сервера, учитывая отличия:

### Docker на свежей Ubuntu 24 (раздел 0)
```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
```
Ты сейчас в открытой SSH-сессии — членство в группе `docker` в ней ещё не
подхватилось. Применить без переподключения:
```bash
newgrp docker          # или переоткрой SSH-сессию
docker compose version # проверка, что работает без sudo
```

### `.env` (раздел 2) — главное отличие
```bash
cp .env.prod.example .env && chmod 600 .env
```
Поправь в `.env`:

| Переменная | Локально | Почему |
|---|---|---|
| `DEBUG` | **`true`** | При `DEBUG=false` auth-куки ставятся с флагом `Secure` (`app/routers/auth.py`) — браузер шлёт их только по HTTPS. По `http://localhost` логин бы молча не работал. `DEBUG=true` снимает `Secure` → логин по HTTP работает. |
| `SECRET_KEY` | любой ≥32 символов | При `DEBUG=true` строгая валидация ключа отключается, но привычку не теряем: `openssl rand -hex 32`. |
| `SMTP_*` | можно оставить заглушки | Письма локально никуда не уйдут. Не страшно: админ (`bootstrap_admin`) и сид-юзеры создаются с уже подтверждённым email — `/auth/login` под ними работает сразу. Самостоятельная регистрация через `/auth/register` потребует письма — его не будет. |

Пароли PG/Rabbit/MinIO — любые. `DOMAIN` не задавай.

### Фронт (раздел 3)
Так же клонируй `show-ring-frontend` рядом — он собирается локально.

### Запуск (раздел 4)
Та же команда. nginx займёт порт **80** на машине:
```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml --env-file .env up -d --build
```
Если 80-й порт занят (другой веб-сервер) — освободи его или временно поменяй
маппинг nginx в `docker-compose.prod.yml` на `"8080:80"`.

### Раздел 5 (домен и TLS) — ПРОПУСТИТЬ
Локально HTTPS не нужен, остаёмся на `:80`.

### Доступ к приложению
- **С самого ноутбука:** UI — http://localhost/ , API — http://localhost/api/health
- **С другого компьютера через ту же SSH-сессию** (порт наружу открывать не надо
  — пробрось туннелем со своей машины):
  ```bash
  ssh -L 8080:localhost:80 <user>@<ip-ноутбука>
  ```
  затем открой `http://localhost:8080/` в своём браузере — трафик уйдёт в nginx
  на ноутбуке. `DEBUG=true` обязателен и для этого пути (туннель — тоже HTTP).
- **По локальной сети:** nginx слушает `0.0.0.0:80`, поэтому с другого устройства
  в той же сети — `http://<ip-ноутбука>/` (если включён `ufw`: `sudo ufw allow 80`).

### Остановка
```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml down      # данные сохранятся
docker compose -f docker-compose.yml -f docker-compose.prod.yml down -v   # стереть всё
```

---

## Публичный доступ через туннель (Cloudflare / ngrok)

Альтернатива SSH-туннелю и пробросу портов: дать локальному стеку **постоянный
публичный HTTPS-адрес**, не открывая ни одного входящего порта. На машине
запускается демон туннеля, он держит **исходящее** соединение к облаку и
форвардит трафик на локальный nginx (`http://localhost:80`). NAT и файрвол при
этом не мешают — наружу ничего слушать не надо.

### Важно: с туннелем оставляем `DEBUG=false`

Это отличие от прямого локального доступа (localhost / LAN / SSH-туннель), где
нужен `DEBUG=true`. Cloudflare и ngrok **терминируют TLS на своём edge** —
браузер общается по **HTTPS**. Флаг `Secure` на auth-куках зависит от `DEBUG`
(`secure=not settings.debug`, `app/routers/auth.py`), и раз браузер на HTTPS —
Secure-куки принимаются и отправляются нормально.

➡️ Для туннеля в `.env` ставь **`DEBUG=false`** (как в боевом разделе 2), а не
`true`. Хак `DEBUG=true` нужен только для голого HTTP.

### Поправки в `.env` под публичный хост

| Переменная | Значение | Зачем |
|---|---|---|
| `DEBUG` | `false` | Secure-куки по HTTPS-туннелю работают (см. выше). |
| `SECRET_KEY` | реальный, `openssl rand -hex 32` | `DEBUG=false` включает строгую валидацию ключа. |
| `ALLOWED_HOSTS` | `["showtail.example.com"]` | Хостнейм туннеля. `TrustedHostMiddleware` (`app/main.py`) отбивает Host-инъекции, когда список задан; пустой = пускает любой Host. |
| `FRONTEND_BASE_URL` | `https://showtail.example.com` | На него строятся ссылки в письмах верификации (`app/services/auth.py`). Важно, только если шлёшь письма (нужен рабочий SMTP). |

`X-Forwarded-Proto` для самого логина трогать не нужно — флаг куки берётся из
`DEBUG`, а не из схемы запроса. nginx-конфиг править не требуется.

После правок `.env` — перезапусти api: `docker compose -f docker-compose.yml -f
docker-compose.prod.yml --env-file .env up -d api`.

---

### Вариант A — Cloudflare Tunnel (рекомендую для постоянного адреса)

Бесплатно даёт **стабильный** поддомен на твоём домене, заведённом в Cloudflare
(free-план подходит). Демон — `cloudflared`.

**1. Установка на Ubuntu 24:**
```bash
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt-get update && sudo apt-get install -y cloudflared
```

**2. Логин и создание именованного туннеля** (один раз):
```bash
cloudflared tunnel login                 # откроет браузер, выбери свой домен
cloudflared tunnel create showtail       # создаст туннель + creds-файл в ~/.cloudflared/
```

**3. Конфиг `~/.cloudflared/config.yml`:**
```yaml
tunnel: showtail
credentials-file: /home/<user>/.cloudflared/<TUNNEL-UUID>.json
ingress:
  - hostname: showtail.example.com
    service: http://localhost:80     # сюда смотрит локальный nginx
  - service: http_status:404         # обязательное правило-замыкатель
```

**4. Привязать DNS и запустить:**
```bash
cloudflared tunnel route dns showtail showtail.example.com
cloudflared tunnel run showtail
```
Готово — приложение доступно на `https://showtail.example.com` (сертификат
выдаёт Cloudflare автоматически).

**5. Автозапуск как systemd-сервис** (чтобы туннель жил после закрытия SSH):
```bash
sudo cloudflared service install
sudo systemctl enable --now cloudflared
```

> **Разовый показ без своего домена:** `cloudflared tunnel --url http://localhost:80`
> — поднимет туннель на случайном `*.trycloudflare.com` без логина и DNS. URL
> эфемерный (меняется при каждом запуске), `ALLOWED_HOSTS` тогда оставь пустым.

---

### Вариант B — ngrok (быстрее, статический адрес ограничен)

**1. Установка и токен:**
```bash
curl -fsSL https://ngrok-agent.s3.amazonaws.com/ngrok.asc | sudo tee /etc/apt/trusted.gpg.d/ngrok.asc >/dev/null
echo "deb https://ngrok-agent.s3.amazonaws.com buster main" | sudo tee /etc/apt/sources.list.d/ngrok.list
sudo apt-get update && sudo apt-get install -y ngrok
ngrok config add-authtoken <ТВОЙ_ТОКЕН>   # из dashboard.ngrok.com
```

**2. Запуск:**
```bash
ngrok http 80                                          # случайный *.ngrok-free.app
ngrok http --domain=твой-статик.ngrok-free.app 80      # статический (1 домен на free-аккаунт)
```
ngrok выдаёт публичный `https://...`-адрес и терминирует TLS — `DEBUG=false`
так же корректно работает. В `ALLOWED_HOSTS` впиши выданный хостнейм.

---

### Безопасность: ты в публичном интернете

- **Реальные секреты** в `.env` — стек теперь доступен всем. Никаких дефолтных паролей.
- **Rate-limit nginx** включён по умолчанию (зоны `auth`/`general`).
- **Приватный показ:** поверх Cloudflare Tunnel можно включить **Cloudflare
  Access** (Zero Trust) — доступ только по списку email/Google-аккаунтов, без
  правок в приложении. У ngrok аналог — `--basic-auth 'user:pass'` на запуске.
- Закрыл показ — **останови демон** (`Ctrl+C` или `systemctl stop cloudflared`):
  пока он не запущен, снаружи ничего не висит.
