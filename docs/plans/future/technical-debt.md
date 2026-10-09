# Технический долг и отложенные улучшения

Список задач, которые сознательно отложены на отдельные сессии/итерации.
Сгруппированы по приоритету. Каждая задача содержит: **что**,
**зачем**, **как делать (эскиз)**, **зависимости**.

Сделанное (для контекста) — стадии 1–15 + follow-up'ы этапа 14:
outbox-pattern, bootstrap admin, RBAC-decorator на support,
`POST /classifieds/{id}/images`, requeue_stuck_tasks с re-publish.

---

## Среднее (требует решений или ~30–60 мин работы)

### Integration-тесты API через httpx.AsyncClient

**Что:** Покрытие всех роутеров end-to-end тестами через
`httpx.AsyncClient` + `ASGITransport`.

**Зачем:** Unit-тесты этапа 13 (`tests/unit/`) проверяют чистую логику
(правила РКФ, security, хелперы). Бизнес-сценарии (создать выставку →
записать собаку → ввести результаты → опубликовать) тестировать
unit'ами нельзя — слишком много integration-точек.

**Как делать:**
1. `tests/conftest.py`:
   - Фикстура `engine` сессионного scope: отдельная база
     `showtail_test` (создаём в `pytest_configure`, дропаем после).
   - Фикстура `migrate` (autouse, session): `alembic upgrade head`.
   - Фикстура `db` (function scope): открыть транзакцию, передать
     сессию, rollback в teardown — изоляция тестов.
   - Фикстура `client`: `AsyncClient(transport=ASGITransport(app=app))`.
   - `dependency_overrides` для `get_db` → тестовая сессия;
     для `rabbit_service` → mock с in-memory queue.
2. `tests/factories.py`: `factory-boy` фабрики User, Kennel, Dog,
   Show. Минимум обязательных полей + sensible defaults.
3. `tests/integration/`:
   - `test_auth.py`: POST /auth/register, login, refresh, logout.
   - `test_shows.py`: полный цикл create → judges → rings →
     entries → results → publish.
   - `test_classifieds.py`: CRUD + FTS-поиск.
   - `test_documents.py`: POST /catalog/generate → mock RabbitMQ
     proxy капчурит сообщение → тест проверяет содержимое payload.

**Зависимости:** Pytest-postgresql (или ручной create/drop), сама
БД должна быть доступна на CI.

---

### CPC (cost-per-click) тарификация рекламы

**Что:** Расширить рекламную модель: списывать с бюджета не только
за impression (CPM), но и за click (CPC). Кампания может выбирать
модель тарификации.

**Зачем:** CPM удобен для брендовой рекламы (показы). CPC — для
performance-рекламы (заводчик хочет именно переходы на свою страницу).
Бизнес-решение: какие модели предлагать клиентам.

**Как делать:**
1. `AdCampaign`: добавить `pricing_model: enum(cpm, cpc)` +
   `cost_per_click: Decimal`. Default cpm для миграции.
2. `services/ad.record_event`: если `event_type=click` и
   `pricing_model=cpc` — списываем `cost_per_click`.
3. Если `event_type=impression` и `pricing_model=cpc` — НЕ списываем
   (показы в CPC не платные).
4. Дашборд: добавить eCPC = spent/clicks в `CampaignStats`.

**Зависимости:** Маленькая миграция (ALTER TABLE add column +
ADD VALUE в enum, безопасно).

---

### Промо-поднятие объявлений (`POST /classifieds/{id}/promote`)

**Что:** Платная функция «поднять объявление в выдаче на N дней».
Поднятые сортируются выше всех остальных в `/classifieds`.

**Зачем:** Монетизация для заводчиков, у которых много объявлений.

**Как делать:**
1. `Classified`: поле `promoted_until: datetime | None`.
2. POST `/classifieds/{id}/promote` принимает duration_days. Сейчас
   без биллинга — просто проставляет дату. Реальная оплата =
   отдельная задача под платёжный шлюз.
3. `list_classifieds` в репозитории: `ORDER BY
   (promoted_until > now()) DESC, created_at DESC`.
4. Cron-задача в scheduler.py: `archive_old_classifieds` не трогает
   promoted, либо чистит истёкший `promoted_until → NULL`.

**Зависимости:** Платёжный шлюз для реальной оплаты (Stripe/CloudPayments)
— отдельная инфра.

---

### Idempotency fail-closed режим

**Что:** Опциональный per-endpoint флаг «без Redis не выполнять
unsafe-запрос». Сейчас при сбое Redis запрос проходит без защиты —
для платёжных эндпоинтов это опасно.

**Зачем:** Платежи, выдача титулов, биллинг рекламы — операции,
которые лучше отклонить (503), чем выполнить дважды.

**Как делать:**
1. В `IdempotencyMiddleware` добавить «список fail-closed путей»
   (regex/prefix-match из конфига `settings.idempotency_required_paths`).
2. Для путей из списка: при недоступности Redis вернуть 503 вместо
   fail-open пропуска.
3. По умолчанию список пуст — поведение не меняется.

**Зависимости:** Нет.

---

## Крупное / инфраструктурное

### Партиционирование `ad_events` помесячно

**Что:** Перевести таблицу `ad_events` на `PARTITION BY RANGE (created_at)`
с одной партицией на месяц.

**Зачем:** При миллионах событий в день плоская таблица деградирует
по INSERT (раздувание индексов) и SELECT (полный скан истории).
Партиции дают:
- быстрые INSERT'ы (новые строки идут только в "горячую" партицию),
- быстрые SELECT'ы по диапазону (planner отсеивает старые партиции),
- дешёвое удаление истории (DROP PARTITION вместо DELETE WHERE).

**Как делать:**
1. Создать новую партицированную таблицу `ad_events_new`.
2. Скопировать данные `INSERT INTO ad_events_new SELECT * FROM ad_events`.
3. DROP старой, RENAME новой.
4. Procedure для авто-создания партиций на 3 месяца вперёд
   (раз в неделю через scheduler).
5. Procedure для DROP партиций старше 12 месяцев (если такая
   политика хранения).

**Зависимости:** Production окно maintenance — переезд опасен
без dry-run на staging.

---

### Materialized Views для тяжёлых дашбордов

**Что:** Вынести `/admin/analytics/dashboard` и `/top-breeds` в
materialized views с авто-refresh.

**Зачем:** При сотнях тысяч записей подзапросы в dashboard SQL'е
начинают занимать секунды. MView пересчитываются в фоне (раз в
5 минут), запрос к ним моментален.

**Как делать:**
1. `CREATE MATERIALIZED VIEW mv_dashboard_stats AS SELECT ...`.
2. В `repositories/analytics.dashboard` — читать из MView, не из
   подзапросов.
3. Scheduler-задача `REFRESH MATERIALIZED VIEW CONCURRENTLY
   mv_dashboard_stats` раз в N минут.
4. Для top-breeds: MView с агрегатом по `created_at >= now() - 30d`
   (sliding window).

**Зависимости:** На уровне ORM MView выглядит как обычная таблица
read-only — никаких изменений в моделях. Решение: ENV-flag «использовать
MView или прямой подзапрос» на случай отладки.

---

## Безопасность и 152-ФЗ: остаток (октябрь 2026)

Сделано в коде: правовые документы и согласия по 152-ФЗ (этап 20), защита
от злоупотреблений в четыре этапа — анти SMS pumping, лимиты nginx и
приложения, капча ALTCHA, блокировка входа, метрики и оповещения, кэш
статики, «Показать контакты», CSP с nonce на фронте. Ниже — что осталось:
в основном действия на сервере и решения, а не код.

> Подробные отчёты аудита и плана защиты хранятся **вне репозитория**:
> репозитории публичные, а в отчётах описаны ещё не закрытые места
> инфраструктуры. Здесь — только список задач без деталей уязвимостей.

### Деплой и сервер (вручную, при публичном запуске)

- **Домен и HTTPS** → затем `HSTS_ENABLED=true`, раскомментировать TLS-блок и
  сервер-заглушку `return 444` в `deploy/nginx/conf.d/show-ring.conf`
  (заглушку — только вместе с доменом: сейчас сайт открывается по IP).
  `upgrade-insecure-requests` в CSP фронт добавит сам по `X-Forwarded-Proto`.
- **DDoS-защита** у хостинга или сервиса в РФ: `deploy/nginx/snippets/real_ip.conf`
  + закрытие прямого доступа через `DOCKER-USER` — `docs/deployment-linux.md`, 5.3.
  Провайдера добавить в раздел 8 Политики конфиденциальности.
- **Hardening сервера** — `docs/deployment-linux.md`, 5.1 (SSH по ключу,
  fail2ban, автообновления, лимит логов Docker).
- **Канал оповещений:** `ALERT_TELEGRAM_BOT_TOKEN` + `ALERT_TELEGRAM_CHAT_ID`
  или `ALERT_EMAIL` (5.2). После запуска подстроить пороги `ALERT_*`.
- **SMS-шлюз:** в кабинете запретить международные направления и задать
  лимит расходов; подобрать `SMS_DAILY_BUDGET` под реальный трафик.
- **`UVICORN_LIMIT_CONCURRENCY`** — поднять при росте онлайна (WebSocket
  считаются в лимит).
- **`NEXT_PUBLIC_SITE_URL`** на фронте — адрес для `sitemap.xml`/`robots.txt`.
- **Незакрытые пункты внутреннего аудита безопасности от 03.10.2026**
  (№ 3, 11, 12, 16 и остаток № 14) — CI/деплой, бэкапы, цепочка поставки,
  конфигурация инфраструктуры. Детали — в непубличном отчёте.
- Рассмотреть перевод репозиториев в приватные.

### 152-ФЗ и юридическое (не код)

- Реквизиты оператора в `show-ring-frontend/src/sections/legal/operator.ts`
  (пока там плашки «[заполнить: …]» на страницах документов).
- Уведомление Роскомнадзора (ст. 22 152-ФЗ), назначение ответственного за
  обработку ПДн, внутренние документы (положение, уровень защищённости по
  ПП РФ № 1119, журнал инцидентов).
- Договор с ОРД для маркировки рекламы.
- Лицензия шаблона Minimal UI под SaaS; права на изображения в `public/assets`.
- Разрешение РКФ на формы и логотипы в `app/templates/documents/*.docx`.
- Решение по полям Instagram/Facebook в профиле.
- Архив предыдущих редакций документов (обещан в п. 18.3 Соглашения) и,
  при необходимости, EN-версия «для удобства».

### Код: доработки по итогам

**Показ рекламы на фронте.** Бэкенд отдаёт в `/ads/serve` пометку «Реклама»,
`erid` и рекламодателя и не показывает баннер без `erid`, но компонента
показа баннеров на фронте нет. При появлении — выводить пометку на самом
баннере (ст. 18.1 Закона о рекламе).

**Капча сильнее proof-of-work.** ALTCHA делает массовые запросы дорогими,
но не отличает человека от бота. Если оповещения покажут целевую атаку на
SMS — подключить Yandex SmartCaptcha (данные в РФ; добавить в Политику)
как второй уровень. Капча пока не стоит на `/users/me/phone/send-code`
(привязка номера вошедшим пользователем — держат лимиты).

**CSP: стили.** `style-src 'unsafe-inline'` оставлен из-за MUI/emotion.
Ужесточить: nonce в emotion-кэш (`AppRouterCacheProvider options.nonce`)
и отказ от атрибутов `style` — заметная переделка, выигрыш невелик.
Полезнее — эндпоинт `report-uri` для сбора нарушений CSP в проде.

**Метрики.** Счётчики живут в Redis поминутно (`app/services/security_metrics.py`).
Если появится Prometheus/Grafana — отдать те же счётчики в формате
Prometheus, логика сбора не меняется.

**sitemap.xml.** Сейчас в нём статические страницы, выставки и питомники.
Добавить собак и объявления; при сборке в Docker API недоступен — карта
наполняется при первой ревалидации (раз в сутки).

**Логи nginx.** Ротация по объёму (5 × 20 МБ), а не по времени: срок
хранения IP в логах формально не фиксирован. Если понадобится точный срок —
отдельный лог-сервис с TTL или анонимизация IP в `log_format`.

---

## Не код, а бизнес/деплой

Эти пункты — config-only или вне приложения. Перечислены, чтобы не
забыть при выкатке.

### CORS allow_origins при появлении домена

В `.env`: `CORS_ALLOW_ORIGINS=["https://showtail.example", "https://admin.showtail.example"]`.
До этого — пустой список = CORS не активируется (защита от случайной
открытости API).

### HSTS / CSP при выкатке за HTTPS

`HSTS_ENABLED=true`, `CSP_ENABLED=true`. Сначала проверить, что
весь трафик уже на HTTPS — иначе HSTS заблокирует HTTP-fallback.

### forwarded_allow_ips (CIDR прокси)

`FORWARDED_ALLOW_IPS=["10.0.0.0/8", "172.16.0.0/12"]` для CIDR'ов
nginx/k8s ingress. Cloudflare даёт публичные IP-диапазоны через
их API. Без этого X-Forwarded-For игнорируется и rate-limit
бьёт по IP реверс-прокси.

### TrustedHostMiddleware

`ALLOWED_HOSTS=["api.showtail.example", "*.showtail.example"]`.
Защищает от Host header injection при misconfiguration nginx.

### RabbitMQ deploy: пересоздание очередей под DLX (follow-up bug_239)

В рамках bug_239 (deep-audit 2026-05-28) добавлен общий DLX/DLQ и
все workflow-очереди (`document_task`, `email_tasks`, `ad_events`,
`tasks`, `showtail.events.dispatcher`) теперь декларируются с
`x-dead-letter-exchange` / `x-dead-letter-routing-key`.

**Что нужно сделать ОДИН РАЗ при первом деплое после этого фикса**
на existing RabbitMQ-кластере:

1. Остановить producers и consumers.
2. Удалить старые очереди без DLX-аргументов:
   ```
   rabbitmqctl delete_queue document_task
   rabbitmqctl delete_queue email_tasks
   rabbitmqctl delete_queue ad_events
   rabbitmqctl delete_queue tasks
   rabbitmqctl delete_queue showtail.events.dispatcher
   ```
3. Поднять воркеры — они сами объявят DLX+DLQ+очереди с новыми
   аргументами.

**Иначе**: RabbitMQ кинет `PRECONDITION_FAILED — inequivalent arg`
при declare, и воркер не стартанёт. На свежем dev-кластере / в
Docker (с anonymous volume) проблемы нет.

**Алертинг (на ops-этап):** мониторить размер DLQ через
RabbitMQ Management API — растущая DLQ = индикатор проблемного
деплоя или poison-payload'ов.

### End-to-end `docker compose up --build` smoke-test

После любых изменений в `Dockerfile`/`docker-compose.yml` —
`docker compose -f docker-compose.yml -f docker-compose.dev.yml
up --build`. Запросы по health-check (`curl
http://localhost:8000/health/ready`), создание admin через
bootstrap_admin, проверка end-to-end (создать выставку → PDF).
~3–5 минут на полный цикл.

---

## Меньшее (одна-две строки или мелкий рефакторинг)

### Grant operator-роли через UI/админку

Эндпоинт `PUT /admin/users/{id}/role` уже принимает любую роль
включая operator (после follow-up'а этапа 11). Не хватает
admin-UI «список кандидатов» (всех breeder/judge без operator-роли).
Минорный QoL.

### Cleanup старых failed outbox-записей

Outbox-таблица будет расти на failed строках при долгой проблеме
с Rabbit. Добавить в scheduler: `DELETE FROM outbox_events WHERE
status='failed' AND created_at < now() - interval '30 days'`.
Раз в неделю.

### Полный flow подтверждения смены email (follow-up bug_203)

Текущий фикс bug_203 (`app/routers/users.py:32`) делает минимум:
требует current_password, помечает email как неподтверждённый и
отзывает все refresh-токены. Но новый адрес НЕ верифицируется
письмом — атакующий мог ввести любой email (включая опечатанный),
и пользователь получит is_email_verified=false с непроверенным
значением в колонке email.

**Полный flow (отдельный PR):**
1. PUT /users/me со сменой email НЕ применяет email сразу. Вместо
   этого создаёт запись в `email_change_tokens` (новая таблица или
   расширение `email_verification_tokens` колонкой `new_email`).
2. На СТАРЫЙ email шлётся уведомление «была попытка смены, если
   это не вы — нажмите undo (одноразовый токен на восстановление)».
3. На НОВЫЙ email шлётся подтверждение со ссылкой
   `/users/me/email-change/confirm?token=...`. Применение email
   только после клика на эту ссылку.
4. До подтверждения старый email остаётся активным; в БД ничего
   не меняется кроме новой строки в email_change_tokens.

**Зависимости:** email-worker уже шлёт письма; нужна миграция
под таблицу/колонку.

### Re-publish stuck task на `/documents` (bug_207)

`app/routers/documents._publish_task` пишет Task в БД и
публикует в RabbitMQ. При сбое publish задача остаётся `pending`
без шанса исполниться. Комментарий в коде обещает admin-эндпоинт.

Решение: использовать существующий outbox-pattern (`app/models/
outbox.py`) — публикация в Rabbit идёт через outbox, fallback'ный
`outbox_publisher_worker` догоняет недоставленные строки.
Конкретно для document-задач — переключить `_publish_task` с
прямого `rabbit_service.publish` на запись в outbox.
