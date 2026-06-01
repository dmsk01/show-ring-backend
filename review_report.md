# Code Review Report

**Date:** 2026-06-01
**Project:** ShowTail — Платформа управления выставками животных (FastAPI / SQLAlchemy 2.0 async / RabbitMQ / Redis / MinIO / Pydantic v2)
**Scope:** Полный обзор `/app`, `/worker`, миграций, тестов, Docker-конфигурации. Встроенный `/review` по всему проекту + расширенный анализ (security / Python / performance / architecture / testing). Версии зависимостей прочитаны из `requirements.txt` / `requirements-dev.txt` (не предполагались). Inline-комментарии прочитаны и учтены; где автор задокументировал осознанный trade-off — он процитирован.

---

## Executive Summary

Кодовая база **зрелая** и заметно прогрессировала с прошлого аудита (`docs/reviews/`, `review_report.md` от 2026-05-28). Практически **все P0/P1 из прошлого отчёта закрыты и подтверждены чтением кода**:

- ✅ Verification-токен больше не утекает в prod-лог — обёрнут в `if settings.debug` (`app/services/auth.py:60`).
- ✅ `_DUMMY_BCRYPT_HASH` стал ленивым (`app/utils/security.py:40-43`).
- ✅ ErrorHandler переведён на FastAPI `exception_handlers` — охватывает весь middleware-стек (`app/main.py:102`).
- ✅ CORS: wildcards заменены явными списками методов/заголовков (`app/main.py:141-147`).
- ✅ PDF: пользовательский ввод эскейпится через `_esc` / `xml.sax.saxutils.escape` (`app/utils/pdf.py`).
- ✅ Email-fallback эскейпит `template_name` (`app/services/email.py:92`); autoescape включён для `.j2`.
- ✅ IDOR на скачивании результата задачи закрыт (`app/routers/tasks.py:147`).
- ✅ Idempotency: добавлен лимит размера кэшируемого тела (256 KB).
- ✅ Архивация classifieds по `GREATEST(created_at, updated_at)` (`app/services/scheduler.py:330`).
- ✅ Document worker: INSERT `UploadedFile` + `mark_done` в одной транзакции (`worker/handlers/document_handler.py:218-249`).

**Новые находки этого ревью** касаются в основном кода, появившегося ПОСЛЕ прошлого аудита (официальные DOCX-документы РКФ, асинхронная обработка изображений), и пары хвостов, которые прошлый отчёт пометил, но фикс не доехал.

**Главные риски сейчас:**

1. 🟠 **`requeue_stuck_tasks` молча теряет новые типы задач** (официальные документы + `process_image`): карта `_QUEUE_FOR_TASK_TYPE` неполная → задача переводится в `pending`, но в очередь не публикуется и не подбирается заново. Stuck-задача умирает тихо.
2. 🟠 **`GET /files/{id}` отдаёт сгенерированные официальные документы без авторизации** — несогласованность ACL: тот же файл через `/tasks/{id}/download` защищён (автор/admin), а через `/files/{id}` — публичен. Документы содержат ПДн (ФИО владельца/заводчика, чип, клеймо, дата рождения).
3. 🟠 **N+1 в билдерах официальных документов** (`app/services/document_official.py`): каталог на 1000 собак = тысячи последовательных `db.get`.
4. 🟠 **`upload_file` всё ещё буферизует файл целиком в RAM** — комментарий теперь прямо противоречит коду.
5. 🟡 **Тестовое покрытие по-прежнему unit-only** — нет интеграционных тестов, тестов middleware/репозиториев, и нет ни одного теста на новый код (официальные документы, image-варианты покрыты лишь частично).

В целом проект близок к prod-готовности; ниже — конкретика.

---

## Critical Issues 🔴

Новых проблем критической severity не обнаружено — все критические находки прошлого аудита (token-logging, PDF-injection, email XSS) подтверждённо устранены. Самый приоритетный security-вопрос (`GET /files/{id}` без auth для документов с ПДн) вынесен в Major #1, поскольку практическая эксплуатируемость ограничена неугадываемым `uuid4` `file_id`; при ужесточении модели данных его стоит переоценить до Critical.

---

## Major Issues 🟠

### [RELIABILITY] `requeue_stuck_tasks` не перепубликовывает новые типы задач — они тихо умирают
- **File:** `app/services/scheduler.py:297-301`, использование `app/services/scheduler.py:254-268`
- **Description:** Карта типов→очередь содержит только три старых типа:
  ```python
  _QUEUE_FOR_TASK_TYPE = {
      "generate_catalog": "document_task",
      "generate_diploma": "document_task",
      "generate_diplomas_batch": "document_task",
  }
  ```
  Но система с тех пор обзавелась пятью официальными типами (`generate_catalog_official`, `generate_diploma_official`, `generate_diplomas_batch_official`, `generate_ring_sheets_official`, `generate_certificates_official` — см. `app/schemas/task.py:63-71`) и типом `process_image` (`app/routers/files.py:40`). Для них `_QUEUE_FOR_TASK_TYPE.get(task.type)` вернёт `None`. В цикле `task.status` уже выставлен в `pending` **до** проверки карты, после чего идёт `continue` без `outbox_repo.enqueue`. Итог: зависшая задача официального документа или обработки изображения переводится в `pending`, но никогда не публикуется в очередь и больше не попадает под `requeue` (он выбирает только `processing`). Задача умирает молча — клиент вечно поллит `done`, которого не будет.
- **Note:** Inline-комментарий честно описывает «для типов, которых нет в карте, перепубликацию пропускаем … можно перезапустить вручную», но (а) это новые штатные типы, а не экзотика, и (б) перевод в `pending` ДО проверки делает даже ручной перезапуск нетривиальным (статус уже не `processing`).
- **Suggestion:** Дополнить карту всеми актуальными типами (официальные → `document_task`, `process_image` → `image_task`), а лучше — строить её из `DocumentKind` + явной константы image-очереди, чтобы новый тип нельзя было забыть. Альтернативно: выставлять `pending` только если очередь найдена.

### [SECURITY] Несогласованный ACL: `GET /files/{id}` отдаёт официальные документы с ПДн без авторизации
- **File:** `app/routers/files.py:135-159` (нет `Depends(get_current_user)`), ср. `app/routers/tasks.py:147` (защищено)
- **Description:** `_upload_and_register` (`worker/handlers/document_handler.py:236`) сохраняет сгенерированные дипломы/каталоги/сертификаты как обычный `UploadedFile`. Эти документы содержат персональные данные: ФИО владельца и заводчика, номер чипа, клеймо, дату рождения собаки, № родословной. Скачивание через `/tasks/{id}/download` корректно ограничено автором задачи или admin (bug_201). Но **тот же файл доступен по `GET /files/{file_id}` вообще без аутентификации** — эндпоинт публичный «чтобы браузер рендерил аватары». Две точки доступа к одному объекту с разным ACL = классический обход контроля доступа.
- **Note:** Комментарий в шапке файла (`app/routers/files.py:7-10`) обосновывает публичность тем, что «фото собак публичны», но он написан до того, как документы стали храниться как `UploadedFile`. Сейчас инвариант «всё в UploadedFile публично» нарушает приватность документов.
- **Mitigation факт:** `file_id` — `uuid4`, не перечисляется и не выдаётся не-владельцу, так что практический вектор узкий. Но это defense-in-depth дыра.
- **Suggestion:** Разделить namespace: для документов (`folder="documents"`/`variants`) либо требовать auth + проверку владельца на `/files/{id}`, либо хранить признак приватности на `UploadedFile` (`is_public`/`visibility`) и отдавать приватные только владельцу/admin. Минимально — пометить документы приватными и проверять в `get_file`.

### [PERFORMANCE] N+1 запросы в билдерах официальных документов
- **File:** `app/services/document_official.py:567-612` (`build_catalog_context`), аналогично `build_diplomas_batch_context:206-225`, `build_ring_sheets_context:325-345`, `build_certificates_context:765-805`
- **Description:** В цикле по записям выставки на КАЖДУЮ запись выполняется по 6–10 последовательных `await db.get(...)`: `Dog`, `Breed`, `BreedGroup`, `ShowClass`, `_resolve_breeder` (Kennel + User + profile), `_resolve_owner` (Kennel + User + profile), отец, мать. Для каталога на 1000 собак это ~8–10 тысяч round-trip'ов к PG, выполняемых строго по очереди (нет `gather`, нет `selectinload`). Identity-map SQLAlchemy частично амортизирует повторные породы/питомники, но собаки/записи уникальны. На реальной всероссийке это десятки секунд в воркере на одну генерацию.
- **Note:** Комментарии описывают доменную логику, но стоимость обхода не упомянута.
- **Suggestion:** Загружать связи пакетно: один `select(ShowEntry).where(show_id==...).options(selectinload(ShowEntry.dog).selectinload(Dog.breed)...)`, либо собрать множества id и сделать `WHERE id IN (...)` одним запросом на таблицу, затем резолвить из словарей. Это переводит N+1 в O(1) запросов на тип сущности.

### [PERFORMANCE/MEMORY] `upload_file` собирает весь файл в память; комментарий противоречит коду
- **File:** `app/services/file_storage.py:126-157`
- **Description:** Цикл читает чанки в `chunks: list[bytes]`, затем `body = b"".join(chunks)` и `put_object(Body=body)`. Файл полностью лежит в RAM. Комментарий на строке 126 утверждает обратное: «Читаем чанками, чтобы **не держать весь файл в памяти**» — но `join` именно держит. Счётчик `total` спасает от OOM сверх лимита (10 МБ), так что катастрофы нет, но при 100 параллельных загрузках это ~1 ГБ RSS, и комментарий вводит в заблуждение. Симметрично `get_file_stream:201-212` читает объект целиком (`await obj["Body"].read()`), а `download_task_result` (`app/routers/tasks.py:170-174`) отдаёт это одним `yield` — `StreamingResponse` не стримит. Комментарии в обоих местах честно помечают это как «переедем на iter_chunks (этап 14)», но этап 14 уже накатан по сетке миграций.
- **Suggestion:** `aioboto3` client поддерживает `upload_fileobj` (стримит file-like) и `obj["Body"].iter_chunks()`. Перевести `upload_file` на `upload_fileobj`, `get_file_stream` — на async-генератор, `download_task_result` — на прямую передачу генератора в `StreamingResponse`. Заодно поправить комментарий на 126.

### [SECURITY] Декодирование пользовательского изображения без явного лимита на «бомбу»
- **File:** `app/utils/image_processing.py:49-51`, вызов `worker/handlers/file_handler.py:82`
- **Description:** `make_variant` делает `Image.open(io.BytesIO(image_bytes))` + `exif_transpose` над байтами, пришедшими из пользовательской загрузки. Декомпрессионная бомба (сильно сжатый PNG/WebP 20000×20000 в пределах 10 МБ лимита) при декодировании развернётся в сотни МБ/несколько ГБ в RAM воркера. Pillow по умолчанию бросает `DecompressionBombError` при превышении `~2× MAX_IMAGE_PIXELS` (~178 Мп), так что воркер не упадёт намертво (ошибка ловится в `process_image_task` → `mark_failed`), но (а) защита неявная и зависит от дефолта Pillow, (б) даже допустимые крупные изображения тратят память до проверки.
- **Note:** Магические байты на загрузке валидируются, но они не ограничивают пиксельные размеры.
- **Suggestion:** Явно задать `Image.MAX_IMAGE_PIXELS` под доменный потолок (например, 50 Мп) в `image_processing.py` и/или проверять `img.size` до полного декода. Сделать guard явным, не полагаясь на дефолт библиотеки.

### [TESTING] Покрытие узкое; новый код почти не покрыт
- **Files:** `tests/` (8 unit + 3 security/service файла)
- **Description:** Есть хорошие unit-тесты: `test_official_context.py`, `test_official_templates.py`, `test_docx_render.py`, `test_image_processing.py`, `test_show_rules.py`, `test_names.py`, `test_security.py`, `test_ad_helpers.py`, плюс security (`test_auth_security`, `test_sanitization`, `test_schemas_security`). НЕ покрыто:
  - rate-limit Lua (`progressive_ban.py`) — критичен, чистая бизнес-логика бана;
  - idempotency middleware (in-flight lock, identity-namespacing, лимит тела);
  - `requeue_stuck_tasks` / `_QUEUE_FOR_TASK_TYPE` — где как раз сидит Major #1; тест «stuck official-doc task перепубликована» поймал бы баг;
  - outbox dispatcher (per-event commit, backoff);
  - репозитории и любой integration-test через `httpx.AsyncClient` + реальный/контейнерный PG;
  - ACL `get_file` vs `download_task_result` (Major #2).
- **Suggestion:** Добавить как минимум: тест карты `_QUEUE_FOR_TASK_TYPE` на полноту по `DocumentKind`; middleware-тесты idempotency/rate-limit; один happy-path integration-тест на upload→variants. ruff + mypy/pyright в CI (см. Minor).

---

## Minor Issues 🟡

### [PERFORMANCE/CORRECTNESS] Sanitization middleware прогоняет bleach по ВСЕМ строкам и пересобирает JSON на каждый запрос
- **File:** `app/middleware/sanitization.py:32-33, 47-59`
- **Description:** `bleach.clean(value, tags=[], strip=True)` применяется рекурсивно ко всем строковым значениям любого `application/json`-запроса. Помимо XSS-тегов это **молча мутирует легитимные данные**: описание собаки `"Чёрный & белый"` или кличка `"A<B"` превратятся в `"Чёрный &amp; белый"` / `"A"`. Плюс на каждый JSON-запрос идёт `json.loads` + полный рекурсивный обход + `json.dumps` — фиксированный налог на горячем пути.
- **Note:** Sensitive-поля исключены (правильно), но контентные поля — нет.
- **Suggestion:** Рассмотреть санитизацию точечно на уровне Pydantic-валидаторов конкретных полей (description/bio), а не глобально на сырых байтах. Минимально — задокументировать, что экранирование `&/<` ожидаемо, чтобы не словить «почему в БД &amp;».

### [ARCHITECTURE] Дублирование строковых литералов типов задач и очередей
- **File:** `app/services/scheduler.py:297`, `app/schemas/task.py:63-71`, `app/routers/files.py:39-40`, `worker/main.py`
- **Description:** Имена типов/очередей раскиданы строковыми литералами по нескольким модулям и должны вручную держаться синхронными (см. Major #1 — рассинхрон уже привёл к багу).
- **Suggestion:** Единый источник: маппинг `DocumentKind → queue` рядом с enum; image-тип/очередь — константы из одного места.

### [TECH-DEBT] Legacy in-memory `task_storage` сосуществует с DB-tasks
- **File:** `app/services/task_storage.py`, `app/routers/tasks.py:66-117`
- **Description:** Учебный pika-пример (`/tasks/send`, `/tasks/{id}/status`, in-memory fallback в `GET /tasks/{id}`) живёт рядом с боевой DB-моделью. Двойной путь усложняет чтение и оставляет потенциальную путаницу ID-пространств.
- **Suggestion:** После того как все клиенты переехали на DB-tasks — удалить legacy-ветку (отмечено и в прошлом отчёте, рекомендация #16).

### [PYTHON] Импорты внутри функций (косметика)
- **Files:** `app/services/email.py:137` (`import re` в `_strip_html`), точечные `import` в теле функций в `scheduler.py`/`notification.py` (по прошлому отчёту).
- **Description:** Циклов в этих местах нет — импорт можно поднять в шапку; микро-стоимость на каждый вызов + ruff `E402`-подобный запах.
- **Suggestion:** Поднять в module-level.

### [TOOLING] ruff/mypy в зависимостях, но без признаков CI-гейта
- **File:** `requirements-dev.txt:17-18`, отсутствие CI-конфига в `git ls-files`
- **Description:** `ruff`/`mypy` объявлены, но нет workflow, который бы их прогонял. Часть Minor-находок (неиспользуемые импорты, импорты в теле) ловится автоматически.
- **Suggestion:** Добавить минимальный CI: `ruff check`, `mypy app`, `pytest`. Это дёшево и защищает от регрессий при правке inline-комментариев.

---

## Positive observations ✅

- **Системная вычитка безопасности доведена до конца:** находки прошлого аудита (`bug_XXX audit 2026-05-28`, `review 2026-05-28`) реально закрыты в коде, а не только в TODO — проверено по файлам.
- **Constant-time login** (`dummy_verify_password`, ленивый кэш) + единое сообщение «Неверный email или пароль» + user-enumeration-safe `register_user` (тихий `None` на занятый email через `IntegrityError`).
- **Refresh-token rotation с reuse-detection** (`app/services/auth.py:151-204`): повторное предъявление отозванного токена аннулирует всю цепочку юзера.
- **Rate-limit атомарным Lua-скриптом** с per-call `fail_closed` для auth-эндпоинтов (`app/middleware/progressive_ban.py`) — корректный trade-off доступность/безопасность.
- **Transactional outbox** с per-event commit, экспоненциальным backoff и `SELECT FOR UPDATE SKIP LOCKED` (`worker/handlers/outbox_handler.py`) — архитектурно правильный at-least-once.
- **Distributed lock** для cron-задач (`_scheduler_lock`) — при репликах>1 не дублирует UPDATE'ы.
- **Magic-bytes валидация** загрузок вместо доверия `Content-Type` (`file_storage._detect_file_type`).
- **CPU-bound вынесен в поток:** и Pillow-варианты (`asyncio.to_thread(make_variant, …)`), и docxtpl-рендер (`_render_official`) не блокируют event loop воркера.
- **PDF-эскейп пользовательского ввода** (`_esc`) с явным комментарием, почему markup ставится в шаблоне, а не в данных.
- **RFC 6266 `filename*`** на скачивании — закрывает header-injection через `original_filename` (bug_202), применён единообразно в `files.py` и `tasks.py`.
- **JWT decode** с явными `require_exp`/`require_sub` и проверкой `type == "access"` в `get_current_user`.
- **Идемпотентность генерации вариантов** (`file_handler._generate_variants` сносит прежние варианты в БД+MinIO перед пересозданием).
- **Пул соединений и `pool_pre_ping`/`pool_recycle`** вынесены в конфиг с подробным обоснованием sizing'а (`app/config.py:94-112`).
- **Inline-комментарии с обоснованием решений** — по-прежнему лучшая практика проекта: делают ревью дешёвым и фиксируют «почему так».

---

## Recommendations

**P0 (до prod / до следующей выкатки документов):**
1. Дополнить `_QUEUE_FOR_TASK_TYPE` всеми актуальными типами (официальные docs + `process_image`) и/или не выставлять `pending` при отсутствии очереди — `app/services/scheduler.py`. **(Major #1)**
2. Закрыть `GET /files/{id}` для приватных документов: признак `is_public` на `UploadedFile` либо auth+owner-проверка — `app/routers/files.py`. **(Major #2)**

**P1 (производительность/устойчивость):**
3. Убрать N+1 в `document_official` через `selectinload`/`IN`-выборки. **(Major #3)**
4. Реальный стриминг: `upload_fileobj` + `iter_chunks` + `StreamingResponse`-генератор; поправить вводящий в заблуждение комментарий в `upload_file`. **(Major #4)**
5. Явный `Image.MAX_IMAGE_PIXELS` / проверка размеров перед декодом. **(Major #5)**

**P2 (качество):**
6. Тесты: полнота `_QUEUE_FOR_TASK_TYPE` по `DocumentKind`; middleware (idempotency, rate-limit); integration upload→variants; ACL `get_file`. **(Major #6)**
7. CI-гейт: `ruff` + `mypy` + `pytest`.
8. Пересмотреть глобальную bleach-санитизацию → точечные Pydantic-валидаторы.
9. Единый источник маппинга типов/очередей.

**P3 (опционально):**
10. Удалить legacy `task_storage` после миграции клиентов на DB-tasks.
11. Поднять локальные `import` в шапки модулей.
