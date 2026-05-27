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
