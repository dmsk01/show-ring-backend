# Развёртывание ShowTail на Linux-сервере — пошагово

Инструкция «с нуля»: от подключения к серверу по SSH и установки пакетов до
работающего прод-стека за nginx и засеянной базы. Рассчитана на то, что Linux ты
видишь впервые — каждая команда с пояснением и проверкой результата.

Стек собирается **прямо на сервере** (`docker compose up --build`), без скачивания
готовых образов из внешнего реестра — то есть зависимости от GitHub/ghcr нет.

Гайд покрывает два сценария:
- **Боевой сервер** (VPS, свой домен, HTTPS) — Части I–II ниже.
- **Локальный запуск на своей Linux-машине** (без домена, по `http://localhost`) —
  см. [Приложение: локальное развёртывание](#приложение-локальное-развёртывание-на-этой-же-машине).

> Что понадобится: сервер (VPS) с **Ubuntu 22.04/24.04**, доступ к нему (IP-адрес +
> логин/пароль или SSH-ключ — их выдаёт хостинг при создании сервера), и ~2 ГБ
> свободной RAM. Разработка под Windows с hot-reload — отдельная инструкция в
> корневом [`README.md`](../README.md).

> **Соглашение по командам.** Если ты подключаешься под пользователем `root` —
> убирай `sudo` из команд (root и так всё может). Под обычным пользователем —
> оставляй `sudo` как написано. `$` в начале строки в примерах не печатается, это
> приглашение терминала.

---

# Часть I. Подготовка сервера с нуля

## 1. Подключиться к серверу по SSH

SSH — это способ управлять удалённым сервером через терминал. С **своего**
компьютера (на Windows — PowerShell или Git Bash, на Mac/Linux — терминал) выполни,
подставив выданные хостингом логин и IP:

```bash
ssh root@123.45.67.89
```
(`root` — имя пользователя, `123.45.67.89` — IP-адрес сервера).

Что увидишь при первом подключении:
- Вопрос вида `Are you sure you want to continue connecting (yes/no)?` — это сервер
  показывает свой «отпечаток». Напечатай `yes` и Enter (только при первом разе).
- Запрос пароля — введи пароль сервера. **Символы при вводе не отображаются** (даже
  звёздочки) — это нормально, просто печатай и жми Enter.

Если хостинг дал **SSH-ключ** вместо пароля — подключайся так:
```bash
ssh -i путь/к/ключу root@123.45.67.89
```

Признак успеха — приглашение сменилось на что-то вроде `root@server:~#`. Теперь все
команды ниже выполняются **на сервере**, в этой SSH-сессии.

> Не закрывай это окно до конца установки. Если связь оборвётся — просто подключись
> снова той же командой.

---

## 2. Обновить систему

Свежесозданный сервер стоит обновить — это подтянет последние версии пакетов и
заплатки безопасности:

```bash
sudo apt update          # обновить список доступных пакетов
sudo apt upgrade -y      # установить обновления (-y = отвечать «да» на вопросы)
```

Первый раз может занять пару минут. Если в конце попросит перезагрузку
(`*** System restart required ***`) — выполни `sudo reboot`, подожди минуту и
подключись по SSH заново (шаг 1).

---

## 3. Установить Git

Git нужен, чтобы скачать («склонировать») код проекта:

```bash
sudo apt install -y git
git --version            # проверка: должно вывести версию, напр. "git version 2.43.0"
```

---

## 4. Установить Docker и Docker Compose

Весь проект работает в Docker-контейнерах, поэтому вручную ставить Python,
PostgreSQL и т.д. **не нужно** — только Docker. Самый простой способ — официальный
установочный скрипт:

```bash
curl -fsSL https://get.docker.com -o get-docker.sh   # скачать скрипт установки
sudo sh get-docker.sh                                # запустить его
```
Идёт 1–3 минуты, ставит и Docker Engine, и плагин `docker compose`.

Дальше — разреши своему пользователю запускать Docker **без `sudo`** (иначе
придётся писать `sudo` перед каждой docker-командой):

```bash
sudo usermod -aG docker $USER    # добавить себя в группу docker
newgrp docker                    # применить в текущей сессии (или выйди/зайди по SSH)
```

Проверь, что всё работает:
```bash
docker --version          # напр. "Docker version 27.x"
docker compose version    # напр. "Docker Compose version v2.x"  ← с пробелом, это плагин v2
docker run hello-world    # тестовый контейнер; должен напечатать "Hello from Docker!"
```
Если `hello-world` напечатал приветствие — Docker готов.

> Под `root` шаг с `usermod`/`newgrp` можно пропустить — root и так запускает Docker.

---

## 5. Настроить файрвол (ufw)

Файрвол закроет лишние порты и оставит только нужные: SSH (чтобы не потерять доступ),
HTTP (80) и HTTPS (443).

> ⚠️ **Сначала разреши SSH, потом включай файрвол.** Если включить ufw, не разрешив
> SSH, ты потеряешь доступ к серверу. Соблюдай порядок команд:

```bash
sudo ufw allow OpenSSH    # разрешить SSH — ОБЯЗАТЕЛЬНО первым
sudo ufw allow 80/tcp     # HTTP (через него работает сайт и выпуск TLS-сертификата)
sudo ufw allow 443/tcp    # HTTPS
sudo ufw enable           # включить файрвол (спросит подтверждение — напечатай y)
sudo ufw status           # проверка: увидишь список разрешённых портов
```

Порты PostgreSQL/Redis/RabbitMQ/MinIO открывать **не нужно** — они доступны только
внутри Docker-сети, наружу не публикуются.

---

## 6. Настроить доступ к GitHub

Код лежит в двух репозиториях на GitHub. Как их скачать — зависит от того, открытые
они или закрытые:

- **Если репозитории публичные** — ничего настраивать не надо, клонируй по HTTPS
  (это сделаем в шаге 7). Переходи к Части II.
- **Если репозитории приватные** — серверу нужен доступ. Самый простой способ —
  завести на сервере SSH-ключ и добавить его в GitHub:

```bash
ssh-keygen -t ed25519 -C "showtail-server"   # три раза Enter (без пароля на ключ)
cat ~/.ssh/id_ed25519.pub                     # покажет публичный ключ — скопируй ВСЮ строку
```
Затем на сайте GitHub: **Settings → SSH and GPG keys → New SSH key**, вставь
скопированную строку, сохрани. Проверь связь:
```bash
ssh -T git@github.com     # при первом разе спросит fingerprint — напечатай yes
```
Ответ `Hi <логин>! You've successfully authenticated...` означает, что доступ есть.

---

# Часть II. Развёртывание приложения

## 7. Клонировать репозитории (бэкенд + фронтенд)

Оба репозитория должны лежать **рядом** (в одном родительском каталоге) — так
бэкенд-стек найдёт исходники фронта для сборки. Кладём в `/opt`:

```bash
cd /opt
```

**Если репозитории публичные (HTTPS, проще):**
```bash
sudo git clone https://github.com/dmsk01/show-ring-backend.git showtail
sudo git clone https://github.com/dmsk01/show-ring-frontend.git
```

**Если приватные (по SSH, требует шаг 6):**
```bash
sudo git clone git@github.com:dmsk01/show-ring-backend.git showtail
sudo git clone git@github.com:dmsk01/show-ring-frontend.git
```

Чтобы дальше не воевать с правами, сделай каталог своим и зайди в бэкенд:
```bash
sudo chown -R $USER:$USER /opt/showtail /opt/show-ring-frontend
cd /opt/showtail
```

Должна получиться такая раскладка:
```
/opt/
├── showtail/              ← бэкенд (этот репо), отсюда запускаем все команды
└── show-ring-frontend/    ← фронт, его соберёт сервис web
```

> Если назовёшь каталог фронта иначе — поправь путь `build: ../show-ring-frontend`
> в `docker-compose.prod.yml`.

### Про фронтенд: что нужно и чего НЕ нужно

У фронта **нет отдельной процедуры развёртывания** — он разворачивается как часть
бэкенд-стека. Достаточно одного действия выше (склонировать рядом):

- ✅ **Клонировать `show-ring-frontend` рядом** — единственный ручной шаг.
- ✅ **Сборка** — автоматически, частью `docker compose up --build` (сервис `web`).
- ❌ **Свой `.env` фронту задавать НЕ нужно** — образ собирается с рабочими
  дефолтами, а `BACKEND_URL` задаётся в `docker-compose.prod.yml`.
- ❌ **`npm install` / `npm run build` на сервере вручную запускать НЕ нужно** —
  всё происходит внутри Docker-сборки.
- ✅ **Маршрутизация** — через nginx: `/` → фронт, `/api/` → бэкенд.

> README фронта в разделе «Деплой» описывает CI-путь через ghcr — здесь мы
> намеренно собираем локально, ради независимости от внешнего реестра.

---

## 8. Создать `.env` и заполнить секреты

`.env` — файл с паролями и настройками. В репозитории его нет (он секретный),
создаём из шаблона:

```bash
cp .env.prod.example .env
chmod 600 .env           # чтобы файл с секретами читал только владелец
```

Сгенерируй случайные секреты (выполни команды, скопируй вывод):
```bash
openssl rand -hex 32     # → SECRET_KEY
openssl rand -hex 24     # → POSTGRES_PASSWORD
openssl rand -hex 24     # → RABBITMQ_PASSWORD
openssl rand -hex 16     # → S3_ACCESS_KEY
openssl rand -hex 24     # → S3_SECRET_KEY
```

Открой файл редактором (`nano` — простой; сохранить `Ctrl+O` Enter, выйти `Ctrl+X`):
```bash
nano .env
```
и впиши значения:

| Переменная | Чем заполнить |
|---|---|
| `SECRET_KEY` | вывод `openssl rand -hex 32`. При `DEBUG=false` приложение **не запустится**, если ключ короче 32 символов. |
| `POSTGRES_PASSWORD` | случайная строка |
| `RABBITMQ_PASSWORD` | случайная строка |
| `S3_ACCESS_KEY` / `S3_SECRET_KEY` | случайные строки (доступ к внутреннему хранилищу файлов MinIO) |
| `DEBUG` | `false` (уже стоит в шаблоне — не менять) |
| `SCHEDULER_ENABLED` | `true` (фоновые задачи: дедлайны выставок и т.п.) |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_FROM_EMAIL` | данные **реального** почтового сервиса (Sendgrid/Mailgun/SES). Без рабочего SMTP не будут уходить письма верификации. |

`POSTGRES_DB`/`POSTGRES_USER` оставь `showtail`. `COOKIE_PATH_PREFIX=/api` уже
задан — не трогай (без него не работает вход).

> **Зачем секреты прокидываются явно?** `.env` не попадает внутрь Docker-образа,
> поэтому всё нужное приложению `docker compose` передаёт в контейнеры отдельно.
> Поменял `.env` → перезапусти сервис, чтобы подхватил.

---

## 9. Собрать образы и запустить стек

Одна команда соберёт бэкенд и фронт и поднимет всё (база, очереди, хранилище,
api, воркеры, nginx):

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml --env-file .env up -d --build
```

Команда длинная (два `-f` файла), поэтому заведи удобный псевдоним — дальше можно
писать просто `dc`:
```bash
echo "alias dc='docker compose -f docker-compose.yml -f docker-compose.prod.yml --env-file .env'" >> ~/.bashrc
source ~/.bashrc
# теперь та же команда короче:
dc up -d --build
```

Что произойдёт автоматически:
1. Соберутся образы бэкенда (`Dockerfile`) и фронта (`../show-ring-frontend`).
   **Первый раз — несколько минут** (ставятся зависимости, собирается Next.js).
2. Поднимутся PostgreSQL, RabbitMQ, Redis, MinIO (наружу не публикуются).
3. Одноразовый контейнер создаст хранилище файлов и накатит миграции БД.
4. Стартуют `api`, воркеры, `web` (фронт) и `nginx`.

Проверь статусы:
```bash
dc ps
```
Все сервисы должны быть `running`/`healthy`; `migrate` и `minio-init` — `exited (0)`
(это одноразовые контейнеры, так и должно быть). Проверь, что приложение отвечает:
```bash
curl -fsSL http://localhost/api/health
```
Ожидаемый ответ — JSON `{"status":"ok", ...}` со всеми компонентами `ok`.

Снаружи приложение уже доступно по `http://<IP-сервера>/` (пока по HTTP, без домена).

> **Email-воркеры** (`worker-events`, `worker-email`, `worker-outbox`) по умолчанию
> не стартуют. Без них письма (верификация, уведомления) копятся в базе и не
> отправляются. Чтобы поднять весь почтовый конвейер, добавь профиль `events`:
> ```bash
> dc --profile events up -d
> ```

---

## 10. Подключить домен и TLS (HTTPS) — опционально

Пока домена нет, всё работает по HTTP. Для боевого режима нужен домен и бесплатный
сертификат Let's Encrypt:

1. **DNS:** в панели регистратора домена заведи A-запись: домен → IP сервера.
   Проверь, что резолвится: `dig +short showtail.example`.
2. В `.env` задай `DOMAIN=showtail.example`.
3. В `deploy/nginx/conf.d/showtail.conf` замени `showtail.example` на свой домен
   (3 места в закомментированном блоке 443 внизу файла).
4. Однократно выпусти сертификат:
   ```bash
   dc run --rm certbot certonly --webroot -w /var/www/certbot \
     -d showtail.example --email admin@showtail.example --agree-tos
   ```
5. Раскомментируй блок `server { listen 443 ssl; ... }` в `showtail.conf`, а в
   блоке `:80` замени `location /` на редирект `return 301 https://$host$request_uri;`.
6. Перезапусти с авто-продлением сертификата:
   ```bash
   dc --profile tls up -d
   ```

---

## 11. Создать первого администратора

После запуска в базе нет администратора (обычная регистрация создаёт рядового
пользователя). Создаём админа командой внутри контейнера `api`:

```bash
dc exec api python -m scripts.bootstrap_admin \
  --email admin@showtail.example --password 'СильныйПароль123!'
```
Команду можно повторять безопасно — дубликат не создаст.

---

## 12. Засеять базу

Скрипты запускаются внутри контейнера `api` (у него есть доступ к базе).

### 12.1. Справочники — ОБЯЗАТЕЛЬНО
Без них (виды животных, породы, выставочные классы, титулы, оценки) приложение
неработоспособно — на них завязаны регистрации и результаты:
```bash
dc exec api python -m scripts.seed_references
```

### 12.2. Демо-данные — ОПЦИОНАЛЬНО (тестовый/демо-стенд)
Наполняет базу примерами по всем разделам — удобно «пощупать» интерфейс. **На
боевом стенде с реальными пользователями обычно НЕ запускают**:
```bash
dc exec api python -m scripts.seed_demo
```

### 12.3. E2E-пользователи — ОПЦИОНАЛЬНО (для тестов фронта)
Пароль у всех — `Password123!`, аккаунты сразу активны:
```bash
dc exec api python -m scripts.seed_e2e_users --force
```

---

## 13. Бэкапы

Сервис `backup` уже включён: ежедневно делает дамп базы, зеркалит файлы и ротирует
старые копии. Они видны на сервере в каталоге `./backups`. Параметры — в `.env`:
```ini
BACKUP_CRON=30 3 * * *        # когда (по умолчанию 03:30)
BACKUP_KEEP_DAILY=7           # сколько дневных копий хранить
BACKUP_KEEP_WEEKLY=4          # сколько недельных
```
Для копий во внешнее облако задай все четыре `BACKUP_S3_*`. Разовый бэкап вручную
(скрипт `backup.sh` лежит на `PATH` контейнера; запускаем одноразовым контейнером,
как документировано в `deploy/backup/backup.sh`):
```bash
dc run --rm backup backup.sh
```

---

## 14. Обновление кода

После выхода нового кода:
```bash
git pull
dc up -d --build      # --build ОБЯЗАТЕЛЬНО, иначе останется старый образ
```
Миграции базы накатятся автоматически при запуске.

---

## 15. Эксплуатация и неполадки

```bash
dc ps                          # статусы всех сервисов
dc logs -f api                 # логи API в реальном времени (выход — Ctrl+C)
dc logs --tail=50 migrate      # почему упала миграция
dc restart worker worker-files # перезапустить воркеры
dc down                        # остановить всё (данные сохраняются)
dc down -v                     # остановить и СТЕРЕТЬ ВСЕ ДАННЫЕ (чистый старт)
```

- **Стек сам поднимается после перезагрузки сервера** — у сервисов задан автозапуск.
- **`/api/health` отдаёт 503 или таймаут** — отвалилась зависимость. Смотри `dc ps`:
  если сервис в `Exited` — `dc up -d`, затем `dc logs <сервис>`.
- **api/migrate падает с ошибкой про SECRET_KEY** — ключ короче 32 символов.
  Сгенерируй заново: `openssl rand -hex 32`.
- **nginx не стартует, ждёт `web`** — фронт не собрался. Проверь, что репо
  `show-ring-frontend` лежит рядом (шаг 7), смотри `dc logs web`.
- **Вход не работает, куки не ставятся** — проверь `COOKIE_PATH_PREFIX=/api` в `.env`.
  По «голому» HTTP (без домена/TLS) вход не работает специально — см. приложение ниже.

---

## Краткая шпаргалка (TL;DR)

Для тех, кто уже всё это делал и хочет просто команды:
```bash
# Подготовка сервера
sudo apt update && sudo apt upgrade -y
sudo apt install -y git
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER && newgrp docker
sudo ufw allow OpenSSH && sudo ufw allow 80/tcp && sudo ufw allow 443/tcp && sudo ufw enable

# Код (HTTPS-вариант для публичных репозиториев)
cd /opt
sudo git clone https://github.com/dmsk01/show-ring-backend.git showtail
sudo git clone https://github.com/dmsk01/show-ring-frontend.git
sudo chown -R $USER:$USER /opt/showtail /opt/show-ring-frontend
cd /opt/showtail

# Секреты
cp .env.prod.example .env && chmod 600 .env
# nano .env → вписать SECRET_KEY (openssl rand -hex 32), пароли, SMTP

# Запуск
docker compose -f docker-compose.yml -f docker-compose.prod.yml --env-file .env up -d --build

# Админ + справочники + проверка
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec api python -m scripts.bootstrap_admin --email admin@showtail.example --password '...'
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec api python -m scripts.seed_references
curl -fsSL http://localhost/api/health
```

---

## Приложение: локальное развёртывание на этой же машине

Запуск всего стека прямо на своём ноутбуке/ПК с Ubuntu (например, прогнать всё
целиком перед выкаткой на боевой сервер). Это **тот же стек**, с тремя поправками:
без домена/TLS, доступ по `http://localhost`, и `DEBUG=true` (чтобы работал вход по
HTTP — см. ниже).

Делай те же шаги, но с отличиями:

- **Подготовка (Часть I):** SSH-подключение (шаг 1) не нужно — ты уже за машиной.
  Docker ставится так же (шаг 4). Файрвол (шаг 5) для локального запуска не
  обязателен.
- **`.env` (шаг 8) — главное отличие:**

  | Переменная | Локально | Почему |
  |---|---|---|
  | `DEBUG` | **`true`** | При `DEBUG=false` куки входа ставятся с флагом `Secure` (`app/routers/auth.py`) — браузер шлёт их только по HTTPS. По `http://localhost` вход молча не сработает. `DEBUG=true` снимает `Secure`. |
  | `SECRET_KEY` | любой ≥32 символов | При `DEBUG=true` строгая проверка ключа выключена, но привычку не теряем: `openssl rand -hex 32`. |
  | `SMTP_*` | можно заглушки | Письма локально не уйдут. Не страшно: админ и сид-юзеры создаются с уже подтверждённым email — вход под ними работает сразу. |

  Пароли PG/Rabbit/MinIO — любые. `DOMAIN` не задавай.
- **Фронт (шаг 7):** так же клонируй `show-ring-frontend` рядом.
- **Запуск (шаг 9):** та же команда. nginx займёт порт **80** на машине. Если он
  занят — поменяй маппинг nginx в `docker-compose.prod.yml` на `"8080:80"`.
- **Домен и TLS (шаг 10): ПРОПУСТИТЬ.**

### Доступ к приложению
- **С самого ноутбука:** UI — http://localhost/ , API — http://localhost/api/health
- **С другого компьютера через SSH** (порт наружу открывать не надо — пробрось
  туннелем со своей машины):
  ```bash
  ssh -L 8080:localhost:80 <user>@<ip-ноутбука>
  ```
  затем открой `http://localhost:8080/` в своём браузере. `DEBUG=true` нужен и здесь.
- **По локальной сети:** nginx слушает `0.0.0.0:80` → с другого устройства в той же
  сети `http://<ip-ноутбука>/` (если включён ufw: `sudo ufw allow 80`).

### Остановка
```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml down      # данные сохранятся
docker compose -f docker-compose.yml -f docker-compose.prod.yml down -v   # стереть всё
```

---

## Публичный доступ через туннель (Cloudflare / ngrok)

Альтернатива пробросу портов: дать локальному стеку **постоянный публичный
HTTPS-адрес**, не открывая входящих портов. На машине запускается демон туннеля, он
держит **исходящее** соединение к облаку и форвардит трафик на локальный nginx
(`http://localhost:80`). NAT и файрвол не мешают.

### Важно: с туннелем оставляем `DEBUG=false`

Это отличие от прямого локального доступа (где нужен `DEBUG=true`). Cloudflare и
ngrok **терминируют TLS на своём edge** — браузер общается по **HTTPS**. Флаг
`Secure` на куках зависит от `DEBUG` (`secure=not settings.debug`,
`app/routers/auth.py`), и раз браузер на HTTPS — Secure-куки работают.

➡️ Для туннеля ставь **`DEBUG=false`** (как на боевом сервере). Хак `DEBUG=true`
нужен только для голого HTTP.

### Поправки в `.env` под публичный хост

| Переменная | Значение | Зачем |
|---|---|---|
| `DEBUG` | `false` | Secure-куки по HTTPS-туннелю работают (см. выше). |
| `SECRET_KEY` | реальный, `openssl rand -hex 32` | `DEBUG=false` включает строгую проверку. |
| `ALLOWED_HOSTS` | `["showtail.example.com"]` | Хостнейм туннеля. `TrustedHostMiddleware` (`app/main.py`) отбивает Host-инъекции; пустой = пускает любой Host. |
| `FRONTEND_BASE_URL` | `https://showtail.example.com` | На него строятся ссылки в письмах (`app/services/auth.py`). Важно, если шлёшь письма. |

После правок `.env` перезапусти api: `dc up -d api`.

### Вариант A — Cloudflare Tunnel (стабильный адрес, бесплатно при своём домене)

**1. Установка на Ubuntu:**
```bash
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt-get update && sudo apt-get install -y cloudflared
```
**2. Логин и создание туннеля (один раз):**
```bash
cloudflared tunnel login                 # откроет браузер, выбери свой домен
cloudflared tunnel create showtail
```
**3. Конфиг `~/.cloudflared/config.yml`:**
```yaml
tunnel: showtail
credentials-file: /home/<user>/.cloudflared/<TUNNEL-UUID>.json
ingress:
  - hostname: showtail.example.com
    service: http://localhost:80
  - service: http_status:404
```
**4. Привязать DNS и запустить:**
```bash
cloudflared tunnel route dns showtail showtail.example.com
cloudflared tunnel run showtail
```
**5. Автозапуск как сервис (чтобы жил после закрытия SSH):**
```bash
sudo cloudflared service install
sudo systemctl enable --now cloudflared
```
> **Разовый показ без своего домена:** `cloudflared tunnel --url http://localhost:80`
> — случайный `*.trycloudflare.com`, эфемерный. `ALLOWED_HOSTS` тогда оставь пустым.

### Вариант B — ngrok (быстрее, статический адрес ограничен)
```bash
curl -fsSL https://ngrok-agent.s3.amazonaws.com/ngrok.asc | sudo tee /etc/apt/trusted.gpg.d/ngrok.asc >/dev/null
echo "deb https://ngrok-agent.s3.amazonaws.com buster main" | sudo tee /etc/apt/sources.list.d/ngrok.list
sudo apt-get update && sudo apt-get install -y ngrok
ngrok config add-authtoken <ТВОЙ_ТОКЕН>   # из dashboard.ngrok.com

ngrok http 80                                     # случайный *.ngrok-free.app
ngrok http --domain=твой-статик.ngrok-free.app 80 # статический (1 домен на free-аккаунт)
```
ngrok выдаёт публичный `https://...` и терминирует TLS — `DEBUG=false` работает.
В `ALLOWED_HOSTS` впиши выданный хостнейм.

### Безопасность: ты в публичном интернете
- **Реальные секреты** в `.env`, никаких дефолтных паролей.
- **Rate-limit nginx** включён по умолчанию.
- **Приватный показ:** поверх Cloudflare Tunnel — **Cloudflare Access** (доступ по
  списку email). У ngrok аналог — `--basic-auth 'user:pass'`.
- Закрыл показ — **останови демон** (`Ctrl+C` или `systemctl stop cloudflared`).
