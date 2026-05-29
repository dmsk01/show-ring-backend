# Code Review Report

**Date:** 2026-05-28
**Project:** ShowTail — Платформа управления выставками животных (FastAPI / SQLAlchemy 2.0 async / RabbitMQ / Redis / MinIO)
**Scope:** Полный обзор кода `/app`, `/worker`, миграций, тестов, Docker-конфигурации; результаты встроенного `/review` + расширенный security/async/perf/architecture анализ.

---

## Executive Summary

Кодовая база зрелая для проекта, который позиционируется как учебный. По tags `bug_XXX audit 2026-05-28` видно, что недавно была проведена системная вычитка безопасности (rate-limit fail-closed, XSS в email-шаблонах, IDOR в загрузках, refresh-token rotation, idempotency cache identity, ProxyHeaders XFF validation и т. д.). Все находки текущего review проверены против inline-комментариев — где автор уже задокументировал осознанное trade-off'ное решение, оно отмечено цитатой.

**Сильные стороны:** атомарные UPDATE-операции вместо SELECT+UPDATE; transactional outbox для гарантированной доставки событий; DLX/DLQ на workflow-очередях; rate-limit через Lua-скрипт (атомарный); идемпотентные cron-задачи через distributed lock; magic-bytes проверка загружаемых файлов; SHA-256 хеши вместо открытых токенов в БД.

**Главные риски:**

1. 🔴 **Verification-токен пишется в info-лог** (`app/services/auth.py:51`) — placeholder "[DEV]" остался без guard'а по `settings.debug`. В prod-логах JSON-агрегатор сохранит токены, дающие право подтвердить чужой email.
2. 🟠 **Архитектура middleware-цепочки** в `app/main.py` помещает `ErrorHandlerMiddleware` ВНУТРИ Sanitization/Idempotency/SecurityHeaders/ProxyHeaders — их исключения не ловятся и не получают `request_id` в ответе. Inline-комментарий описывает другую модель, чем фактическое поведение Starlette.
3. 🟠 **`_DUMMY_BCRYPT_HASH = pwd_context.hash(...)`** (`app/utils/security.py:17`) вычисляется на импорте модуля и затягивает старт процесса ~250 мс; в тестах при коротких процессах это превращается в стабильный налог.
4. 🟠 **CORS: `allow_methods=["*"]` + `allow_headers=["*"]` при `allow_credentials=True`** (`app/main.py:122-129`) — браузеры по CORS-спецификации отвергают wildcards при credentials; нужны конкретные списки.
5. 🟠 **Тестовое покрытие крайне узкое** — 5 файлов, только authentication + sanitization + utility-функции. Нет integration-тестов с реальной БД/HTTP, нет тестов на репозитории, нет проверки rate-limit/idempotency middleware'ов.

В остальном проект выглядит готовым к prod-выкатке с учётом устранения находок ниже.

---

## Critical Issues 🔴

### [SECURITY] Verification token logged at INFO level
- **File:** `app/services/auth.py:51`
- **Description:** В `register_user` строка `logger.info("[DEV] Verify token for %s: %s", email, raw_token)` пишет одноразовый токен подтверждения email в info-лог. Комментарием выше отмечено `# заглушка отправки почты`, но при `log_json=True` (prod) этот лог уйдёт в ELK/Loki, и любой с доступом к лог-агрегатору сможет подтверждать чужой email. На этапе 9 уже есть email-инфраструктура (`app/services/email.py`, `worker/handlers/email_handler.py`), но `register_user` её не вызывает — half-implemented feature.
- **Note:** В файле явный признак "ещё не доделано", но без guard'а: `if settings.debug: logger.info(...)`. В коммите есть отдельный `security_logger = logging.getLogger("app.security")`, который правильно НЕ пишет токены, — это намёк, что данная строка просто забыта.
- **Suggestion:** Заменить на `publish_event(EventMessage(event_type="user.email_verification_requested", payload={"user_id": ..., "raw_token": raw_token}), db=db)` через transactional outbox (token уже хешированный в БД, и в outbox payload его можно держать ровно столько, сколько нужно воркеру до SMTP-отправки). Минимально допустимый патч — обернуть в `if settings.debug:`.

### [SECURITY] Email рендерится только если шаблон найден; иначе шлёт raw event_type
- **File:** `app/services/email.py:80-86`
- **Description:** Fallback в `render_email` при `TemplateNotFound` возвращает `f"<p>Событие: {template_name}</p>"`. `template_name` приходит из `event.event_type` (см. `worker/handlers/events_handler.py:123`). Сейчас event_type — внутренний enum, но если когда-нибудь источник event'ов расширится на внешний publisher (webhook integration), `event_type` может стать управляемым. autoescape для `j2`-extension включён, но fallback идёт мимо template engine — `template_name` подставляется через f-string в html, без экранирования.
- **Note:** inline-комментарий объясняет fallback как "лучше отдать что-то, чем 500"; защита от inject'а в этом месте отсутствует.
- **Suggestion:** Экранировать `template_name`: `from markupsafe import escape; ... f"<p>Событие: {escape(template_name)}</p>"`. Лучше — отдавать общий статический fallback "Уведомление от ShowTail" без подстановки.

---

## Major Issues 🟠

### [ARCHITECTURE] Middleware order: ErrorHandler не охватывает outer-middleware
- **File:** `app/main.py:94-117`
- **Description:** В Starlette `add_middleware` оборачивает приложение в обратном порядке: последний добавленный — outermost. В этом файле порядок добавления:
  RequestId → ErrorHandler → Sanitization → Idempotency → SecurityHeaders → ProxyHeaders → TrustedHost → CORS.
  При входящем запросе сначала срабатывают outer middleware (CORS → TrustedHost → ProxyHeaders → SecurityHeaders → Idempotency → Sanitization), и только потом ErrorHandler. Значит exception из Sanitization/Idempotency/ProxyHeaders **не попадает** в ErrorHandlerMiddleware — он его не оборачивает.
- **Note:** Inline-комментарий описывает «1. RequestId выполняется первым», что не соответствует факту: первым выполняется ProxyHeaders (после CORS/TrustedHost), а RequestId — самый внутренний. Idempotency-middleware при битом Redis имеет тяжёлый код-path и реально может бросить — это будет 500 от дефолтного Starlette handler'а без request_id.
- **Suggestion:** Переместить `ErrorHandlerMiddleware` (или его эквивалент через `app.add_exception_handler(Exception, ...)`) в **самый внешний** слой. Простейший фикс — переставить `app.add_middleware(ErrorHandlerMiddleware)` в самый конец `add_middleware`-цепочки. Или использовать FastAPI-уровневый `@app.exception_handler(Exception)` — он охватывает весь стек.

### [PERFORMANCE] Module-load bcrypt hash блокирует импорт на ~250 мс
- **File:** `app/utils/security.py:17`
- **Description:** `_DUMMY_BCRYPT_HASH = pwd_context.hash("dummy-password-for-timing")` исполняется при первом `import app.utils.security` и стоит ~250 мс CPU. Этот импорт цепляется почти отовсюду (через `app.services.auth`, `app.dependencies`). Эффект: холодный старт API/worker'а медленнее на десятые секунды; в pytest при сборе фикстур цена платится каждый раз.
- **Note:** Комментарий объясняет необходимость dummy-hash для constant-time проверки в `dummy_verify_password`, но не объясняет, почему он строится на импорте, а не лениво.
- **Suggestion:** Сделать lazy:
  ```python
  _DUMMY_BCRYPT_HASH: str | None = None
  def dummy_verify_password() -> None:
      global _DUMMY_BCRYPT_HASH
      if _DUMMY_BCRYPT_HASH is None:
          _DUMMY_BCRYPT_HASH = pwd_context.hash("dummy-password-for-timing")
      pwd_context.verify("dummy-password-for-timing", _DUMMY_BCRYPT_HASH)
  ```
  Первый вызов login'а с несуществующим email прогреет кэш.

### [SECURITY] CORS wildcards с credentials=True
- **File:** `app/main.py:122-129`
- **Description:** При `allow_credentials=True` спецификация CORS требует, чтобы `Access-Control-Allow-Origin` был конкретным origin (не `*`), а `Allow-Methods`/`Allow-Headers` — конкретными списками. Указание `allow_methods=["*"]` + `allow_headers=["*"]` с credentials — non-compliant: Chrome/Firefox отрежут запросы с реальными origin'ами, кроме самых тривиальных GET.
- **Note:** Inline-комментарий касается только пустого `cors_allow_origins`, но не описывает wildcard в методах/заголовках.
- **Suggestion:** Прописать конкретные списки, например:
  ```python
  allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
  allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID"],
  ```

### [PERFORMANCE/MEMORY] upload_file читает файл целиком в память
- **File:** `app/services/file_storage.py:127-157`
- **Description:** `chunks: list[bytes] = []` и `body = b"".join(chunks)` — файл собирается в RAM перед `put_object`. С `max_upload_size_bytes=10MB` и 100 одновременных загрузок это 1 GB RSS; на крупных файлах (если лимит поднять) — OOM.
- **Note:** Inline-комментарий явно обещает миграцию на multipart в этапе 14: `# На очень больших файлах в этапе 14 переедем на multipart upload (>5 МБ).` — Stage 14 уже пройден по migrations, follow-up забыт.
- **Suggestion:** Использовать `aioboto3.client.upload_fileobj` или MultipartUpload API через `_s3_client()` — он принимает file-like объект и стримит чанки на сервер S3 без сборки `body`.

### [PERFORMANCE] StreamingResponse не стримит — `body` собирается полностью в `routers/tasks.py`
- **File:** `app/routers/tasks.py:169-190`
- **Description:** `body, content_type = await file_storage.get_file_stream(db_file.s3_key)` уже выкачивает PDF целиком в `bytes`. Затем `_iter()` отдаёт его одним чанком. Эффект — никакого реального стриминга: для каталога на 1000 собак ~20-30 МБ держатся в памяти процесса плюс полностью в памяти aioboto3-буфере.
- **Note:** Комментарий говорит «StreamingResponse экономит память для больших файлов» — это верно в теории, но текущая реализация `get_file_stream` нарушает контракт.
- **Suggestion:** Поправить `app/services/file_storage.py:get_file_stream` так, чтобы возвращать async-итератор: использовать `await obj["Body"].iter_chunks()` и `yield chunk`-генератор, а `download_task_result` передавать его напрямую в `StreamingResponse`.

### [SECURITY] PDF rendering не экранирует пользовательский ввод в Paragraph
- **File:** `app/utils/pdf.py:329, 343, 396`
- **Description:** ReportLab Paragraph использует XML-подобную разметку. Значения `dog_name`, `owner_name`, `titles[i]` подставляются прямо в строку. Если пользователь введёт кличку `<i>Bobby</i>` или `<font color="red">X</font>`, ReportLab либо отрендерит markup (поведение, которого автор не ожидал), либо упадёт с ошибкой парсинга при `<` без закрытия. На каталоге выставки на 1000 собак одно битое имя ронит весь PDF.
- **Note:** В комментариях есть упоминание «вёрстка ручная (нет CSS)», но эскейп Paragraph-входа не упомянут.
- **Suggestion:** Прогонять все user-supplied строки через `from xml.sax.saxutils import escape` перед вставкой в `Paragraph(...)`. Альтернатива — `Paragraph(f"Кличка: <b>{escape(data.dog_name)}</b>", ...)`.

### [PERFORMANCE] Idempotency middleware буферизует ответ в память для всех 2xx
- **File:** `app/middleware/idempotency.py:201-243`
- **Description:** Любой 2xx-ответ POST/PUT/PATCH/DELETE с заголовком `Idempotency-Key` буферизуется (`async for chunk in body_iterator: body_chunks.append(chunk)`) и затем кладётся в Redis как JSON. Для эндпоинтов, возвращающих большие JSON-ответы (например, `POST /search` или `POST /admin/...` с агрегациями), это удваивает память на один ответ и хранит до 24 часов копию в Redis.
- **Note:** Комментарий объясняет, почему 2xx кэшируется, но не описывает пределы. Сейчас `download` не идёт через идемпотентность (это GET), но как только появится POST-эндпоинт, возвращающий бинарь, проблема всплывёт.
- **Suggestion:** Добавить лимит размера тела (например, 256 KB), при превышении — пропускать через middleware без кэширования и логировать warning'ом. Можно также skip'ать `Content-Type: application/octet-stream` и `application/pdf`.

### [RELIABILITY] Document worker может оставить файл-сироту в S3 при крэше между upload и mark_done
- **File:** `worker/handlers/document_handler.py:108-113, 191-207`
- **Description:** Шаги: 1) `upload_bytes` в MinIO + INSERT `UploadedFile` + commit; 2) `mark_done` (отдельный commit). Если процесс убьют между ними — `UploadedFile` уже создан и `s3_key` указывает на реальный файл, но `task.status` остался `processing`. Scheduler через час переотправит задачу → новый PDF, новый `UploadedFile` → первый «сиротеет». Атаkers могут злоупотреблять, отправляя задачи и пол-минуты позже отзывая авторизацию для генерации лишних файлов (хотя для этого нужно убить процесс — vector слабый).
- **Note:** Комментарий явно говорит: `# Если процесс упадёт между claim и mark_done, задача останется processing — её можно подобрать отдельным cleanup-job (этап 14)`, но cleanup-джоб не добавлен (по сетке миграций stage 14 уже накатан).
- **Suggestion:** Объединить INSERT `UploadedFile` и `mark_done` в одну транзакцию. Идемпотентность по `task_id` через UNIQUE-индекс `uploaded_files.task_id` → второй UPDATE/INSERT упадёт с IntegrityError; обработчик заметит и не сделает дубль.

### [TESTING] Покрытие критических путей отсутствует
- **Files:** `tests/`
- **Description:** Существующие 5 файлов покрывают: auth-security (хорошо), sanitization (хорошо), schemas-security (тонко), security-utility (хорошо), show-rules unit (хорошо), ad-helpers (хорошо). НЕ покрыто:
  - rate-limit Lua-скрипт (atomic ban — критичен после bug_246/247);
  - idempotency middleware (in-flight lock, identity-namespacing — bug_018);
  - proxy_headers XFF validation (bug_012);
  - file-storage magic-bytes validation;
  - moderation `last_admin_protected` / `cannot_block_self`;
  - outbox dispatcher (per-event commit, backoff);
  - WS auth flow + per-message session (bug_205, bug_206).
  Любой из этих модулей переписывается без сигнала «тест сломался» → высокая вероятность регрессии при ревью inline-комментариев.
- **Suggestion:** Добавить минимум:
  - `tests/middleware/test_idempotency.py` — два запроса с одним ключом, проверка единичной обработки + 409 в гонке;
  - `tests/middleware/test_progressive_ban.py` — sliding window, бан после превышения, экспоненциальный рост;
  - `tests/services/test_moderation.py` — fail-paths self-block и last-admin;
  - integration-test через httpx.AsyncClient + testcontainers (Postgres).

---

## Minor Issues 🟡

### [PYTHON] Неиспользуемый импорт `Decimal` в ad-сервисе
- **File:** `app/services/ad.py:21`
- **Description:** `from decimal import Decimal` — не используется в файле.
- **Suggestion:** Удалить. `ruff check --select F401` поймал бы.

### [PYTHON] Импорты внутри функций (cosmetic)
- **Files:** `app/services/scheduler.py:237, 317`, `app/repositories/notification.py:194, 218`
- **Description:** `from datetime import timedelta` / `import logging` появляются в теле функций. Делается, видимо, чтобы избежать циклов / lazy-импорта, но в этих местах цикла нет. Лишние строки и микро-стоимость на каждый вызов.
- **Suggestion:** Поднять в шапку файла.

### [ARCHITECTURE] Глобальный `redis_client: Redis | None` усложняет тестирование
- **File:** `app/redis.py:11, app/middleware/idempotency.py:38, app/services/ad.py:29, app/services/ws_manager.py:48, app/services/scheduler.py:78`
- **Description:** Большинство сервисов лезут напрямую в модуль-глобал `redis_client` вместо Depends. Для unit-теста любого из этих сервисов нужно подменять глобал через monkeypatch. Это работает, но текущие тесты этим не пользуются, и в будущем добавит трения.
- **Note:** Inline-комментарий нигде не объясняет выбор глобала; вероятно — legacy с этапа 2.
- **Suggestion:** Не рефакторить сейчас, но при росте — завернуть в lazy property с `_get_redis()`, который можно мокать в conftest.

### [PYTHON] `setup_logging` дропает все root-handler'ы — может стереть pytest-плагины
- **File:** `app/logging_config.py:75-77`
- **Description:** `for h in list(root.handlers): root.removeHandler(h)`. На lifespan FastAPI это разумно, в тестах при `asyncio_mode=auto` lifespan не запускается — лог-плагин pytest (caplog) не пострадает. Если когда-то добавится TestClient с lifespan_on, caplog-fixture может потерять handler'ы.
- **Suggestion:** В тестах не запускать `setup_logging()` напрямую; если запускается через lifespan, добавить env-флаг `DISABLE_LOG_RESET`. Сейчас риск низкий — оставить как есть, но иметь в виду.

### [PYTHON] Mutable `Sequence[T]` возвращается из репозиториев как `result.scalars().all()` (это `list`, но аннотация Sequence)
- **Files:** `app/repositories/show.py:78, dog.py:53, litter.py:45, classified.py:96`
- **Description:** Возврат типизирован как `Sequence[T]`, фактически возвращается `list[T]`. Это не баг, но это нарушает LSP: вызывающий код, объявленный по контракту `Sequence`, не может делать `.append`. Не критично, но pyright не предупредит, если кто-то приведёт к `list[T]` через cast.
- **Suggestion:** Сменить аннотацию на `list[T]` — она точнее отражает фактический контракт.

### [PERFORMANCE] `app/repositories/show.py:is_breed_allowed` делает 2 запроса
- **File:** `app/repositories/show.py:126-146`
- **Description:** Сначала COUNT(*) всех breed-записей выставки, потом COUNT(*) совпадающих. Можно одним запросом через `WHERE breed_id = :b OR :b IS NULL → EXISTS`.
- **Suggestion:**
  ```python
  stmt = select(func.bool_and(
      func.coalesce(ShowBreed.breed_id, breed_id) == breed_id
  )).where(ShowBreed.show_id == show_id)
  ```
  Или, читабельнее, один SQL с CASE. Сейчас не bottleneck, но при N выставок на детальной странице — линейно.

### [TYPING] `cast(AbstractAsyncContextManager[Any], ...)` в `_s3_client`
- **File:** `app/services/file_storage.py:88-97`
- **Description:** Тип `Any` теряет type-safety на S3-операции внутри `async with`. pyright не предупредит, если автор вызовет несуществующий метод S3-клиента.
- **Note:** Inline-комментарий объясняет cast наличием неполных stubs у aioboto3 — корректно. Но `Any` — снаряд: лучше cast в `aiobotocore.client.AioBaseClient` через `typing.TYPE_CHECKING`.

### [ARCHITECTURE] `_is_admin` дублируется в 5+ роутерах
- **Files:** `app/routers/classifieds.py:33, ads.py:46, tasks.py:120, shows.py:46, dogs.py (similar)`
- **Description:** Одна и та же функция `def _is_admin(user: User) -> bool: return any(r.role.value == "admin" for r in user.roles)`. Не баг, но плохой запах: если завтра логика «кто такой admin» поменяется (например, `super_admin`), правок в N местах.
- **Suggestion:** Поднять в `app/dependencies.py` как `def is_admin(user: User) -> bool: ...`. Не делать `Depends` — это helper, а не dependency.

### [PYTHON] type:ignore вместо явного типа в `worker/main.py:87`
- **File:** `worker/main.py:87`
- **Description:** `import uuid as _uuid` внутри функции — нестандартный alias только в одном месте.
- **Suggestion:** Поднять `import uuid` в шапку файла.

### [CORRECTNESS] `archive_old_classifieds` использует `created_at < cutoff` без учёта типа статуса
- **File:** `app/services/scheduler.py:309-335`
- **Description:** Архивирует ВСЕ active-объявления старше 90 дней по `created_at`. Если автор недавно обновил объявление (`updated_at`), он мог его «оживить». Архивация по `created_at` это игнорирует.
- **Note:** Комментарий описывает логику архивации, но не упоминает `updated_at`.
- **Suggestion:** Сменить условие на `updated_at < cutoff` (или `GREATEST(created_at, updated_at) < cutoff`), чтобы свежие правки задерживали архивацию.

### [PERFORMANCE] Кеш дашборда: race на cache fill
- **File:** `app/repositories/analytics.py:78-104`
- **Description:** При cache miss каждое одновременное обращение к админ-дашборду заново выполнит тяжёлый SELECT. Stampede-эффект на 5 минут после expiration. Cost — 10× COUNT(*) на 100k+ строках × N админов.
- **Note:** Inline-комментарий касается стоимости запроса, но не stampede protection.
- **Suggestion:** SETNX-лок на 5 секунд `analytics:dashboard:lock:v1` — пока один заполняет, остальные либо ждут с polling'ом, либо возвращают stale (если есть).

### [CORRECTNESS] WS authenticate_ws не проверяет, что user.is_active в момент сообщения дублирует проверку в loop
- **File:** `app/routers/support.py:236-253, 365-388`
- **Description:** В `_authenticate_ws` проверяется `user.is_active`. В цикле — повторно `fresh_user.is_active`. Между двумя проверками только handshake (одно сообщение), поэтому фактически дубль (но не вредит).
- **Note:** Комментарий о per-message session-rotation корректен и обоснован.
- **Suggestion:** Не править — это defense-in-depth, обоснованно.

### [PYTHON] `app/main.py:18-35` импорт всех роутеров явный — длинно, но читабельно (positive)
- (упомянуто в Positive observations).

### [DOC] Mismatched ordering claim in main.py middleware comment
- **File:** `app/main.py:94-105`
- **Description:** См. Major issue выше — комментарий и нумерация противоречат фактическому поведению.
- **Suggestion:** Переписать комментарий, заодно с фиксом порядка.

---

## Positive observations ✅

- **Atomic UPDATE-операции** вместо SELECT+UPDATE: `revoke_refresh_token` (`app/repositories/user.py:62`), `mark_email_token_used`, `increment_views`, `mark_sent`/`mark_failed` (`app/repositories/notification.py`). Всё корректно ловит rowcount=0 как сигнал гонки.
- **Transactional outbox** в `app/repositories/outbox.py` + `worker/handlers/outbox_handler.py` с per-event commit и SKIP LOCKED. Архитектурно правильно: даёт at-least-once без хрупкости distributed transactions.
- **Refresh-token rotation + reuse-attack detection** (`app/services/auth.py:137-190`): на повторное использование отозванного токена отзываются все активные refresh'ы юзера. Defense-in-depth.
- **Rate-limit через Lua-скрипт** (`app/middleware/progressive_ban.py`): атомарное check-and-set, что исправляет реальную гонку в pipeline-вариантах. Per-call `fail_closed` для auth-эндпоинтов — правильный trade-off между UX и security.
- **Idempotency-Key** middleware c in-flight SETNX-локом, namespace по identity (auth header sha256) — закрывает класс cross-user replay-атак (bug_018).
- **ProxyHeaders middleware** валидирует XFF как IP-адрес перед подменой scope (bug_012) — закрывает rate-limit bypass через ротацию заголовка.
- **Magic-bytes validation** на загружаемых файлах (`app/services/file_storage.py:_detect_file_type`) — не доверяем Content-Type клиента.
- **Per-message DB-session в WebSocket** (`app/routers/support.py`, bug_205) — рассказал про реальный инцидент с истощением pool'а; решение архитектурно правильное.
- **Constant-time login** через `dummy_verify_password` (`app/utils/security.py:29`) и единое сообщение `"Неверный email или пароль"` устраняют user enumeration по времени и по тексту.
- **Distributed lock** для cron-задач через SET NX EX + nonce-Lua release (`app/services/scheduler.py:60-120`). При replicas>1 не дублирует UPDATE'ы.
- **DLX/DLQ infrastructure** через `declare_workflow_queue` (`app/services/rabbit_dlx.py`). Идемпотентная декларация во всех точках producer/consumer'а.
- **JWT decode options** явно требуют `require_exp` и `require_sub` (`app/utils/security.py:53-63`) — закрывает класс «токенов без полей».
- **Inline-комментарии с обоснованием решений** (`bug_XXX audit ...`, `ИСПРАВЛЕНО:`) — отличная практика; делает code review дешёвым и помогает future-self не наступить второй раз.
- **Параллельные health-проверки** через `asyncio.gather` (`app/routers/health.py:91`) — не блокирует медленный сервис.
- **Multi-stage Dockerfile** с non-root user'ом, `--no-install-recommends`, копированием готовой venv. Production-ready.
- **Healthcheck + depends_on.condition: service_healthy** в `docker-compose.yml` — API ждёт реальной готовности зависимостей.
- **Scheduler-задачи + outbox**: requeue stuck tasks публикует не напрямую в Rabbit, а через outbox (gh_236) — корректно поддерживает at-least-once.

---

## Recommendations

**P0 (до выкатки в prod):**
1. Убрать или guard'нуть verification-token логирование (`app/services/auth.py:51`) и подключить email-отправку через `publish_event`.
2. Переставить ErrorHandlerMiddleware в самый внешний слой (или использовать `@app.exception_handler`) — `app/main.py`.
3. Поправить CORS: убрать wildcards при `allow_credentials=True` — `app/main.py:122-129`.
4. Объединить INSERT UploadedFile и mark_done в одну транзакцию в document worker — `worker/handlers/document_handler.py:191-207`.

**P1 (стабилизация):**
5. Lazy `_DUMMY_BCRYPT_HASH` (`app/utils/security.py`).
6. Эскейп пользовательских строк в PDF-генерации (`app/utils/pdf.py`).
7. Реальный streaming в `get_file_stream` + `StreamingResponse` (`app/services/file_storage.py`, `app/routers/tasks.py`).
8. Лимит размера ответа в Idempotency-кэше (`app/middleware/idempotency.py`).

**P2 (качество):**
9. Расширить тесты: middleware (idempotency, rate-limit, proxy_headers), сервисы (moderation, outbox), integration-test через httpx + testcontainers.
10. Убрать неиспользуемые импорты (`Decimal` в `app/services/ad.py`); ruff/pyright в CI.
11. Поднять `_is_admin` в общий модуль.
12. Лёгкий рефакторинг: `Sequence[T]` → `list[T]` в репозиториях.
13. Архивация classifieds по `updated_at`, не `created_at` (`app/services/scheduler.py`).
14. Stampede-protection для dashboard-кеша.

**P3 (опционально):**
15. Замена глобального `redis_client` на dependency-injection pattern.
16. Удаление legacy in-memory `task_storage` после миграции всех путей на DB-backed tasks.
