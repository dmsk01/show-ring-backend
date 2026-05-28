# Глубокий аудит проекта — 2026-05-28

**Метод:** 3 параллельных explore-агента (Performance/DB, Worker/Background,
Business+Security) + ручной обход middleware, миграций, конфига пула.
Сводный охват: `app/routers/`, `app/services/`, `app/repositories/`,
`app/models/`, `app/middleware/`, `worker/`, `migrations/versions/`,
`app/database.py`.

**Не покрыто этим ревью:**
- Уже исправленные баги bug_001–019 (предыдущий /ultrareview) и
  bug_201–207 (`docs/reviews/local-review-2026-05-27.md`).
- Frontend / клиентский код (его нет).
- Деплой / docker-compose / CI (отдельная категория, см. tech-debt.md).

**Найдено всего: 40 пунктов** (8 HIGH, 15 MEDIUM, 17 LOW). Дубликаты с
предыдущими ревью отброшены.

## Чек-лист

### HIGH (P0 — фиксить в первую очередь)

- [x] **bug_208** — IDOR: `PUT /shows/{show_id}/results/{result_id}`
  не валидирует, что result принадлежит этому show
- [x] **bug_209** — Race condition в каскаде BoB/BIG/BIS: одновременные
  re-election'ы оставляют stale-флаги без `SELECT ... FOR UPDATE`
- [x] **bug_210** — Classified `status` mass-assignable через PUT
  без валидации state machine transition
- [ ] **bug_230** — Email handler не идемпотентен: повторная доставка
  RabbitMQ-сообщения дублирует Notification и SMTP-отправку
- [ ] **bug_231** — Events handler создаёт Notification + публикует
  email_task в разных шагах: краш между ними = потерянное письмо
- [x] **bug_232** — Outbox batch commit-after-publish: при сбое
  посреди батча #5 ломает атомарность всего #1..#N
- [x] **bug_233** — `task.mark_done()` без `WHERE status='processing'`
  → второй воркер может перезаписать результат первого
- [x] **bug_236** — APScheduler-задачи запускаются на КАЖДОМ инстансе
  FastAPI: при scale=3 stuck-задачи переотправляются 3 раза

### MEDIUM (P1)

- [x] **bug_211** — `target_url` в баннере проходит регэксп
  `^https?://.+` — JS-схема через query-параметр в frontend
- [x] **bug_212** — `classified.add_images` принимает file_id без
  верификации владельца файла → можно прицепить чужие фото
- [x] **bug_213** — Нет rate-limit на `/ads/events` и
  `/classifieds/search`: накрутка показов и DoS на FTS
- [x] **bug_214** — При `campaign.budget=0` (если ввести через DB)
  бесплатные impressions запишутся бесконечно
- [x] **bug_221** — Нет индекса на `show_rings.judge_id` (FK без индекса)
- [x] **bug_222** — Нет индекса на `show_entries.handler_id`
- [x] **bug_223** — Нет индекса на `dog_titles.judge_id`
- [x] **bug_224** — `ORDER BY random()` в подборе баннера: O(n log n)
  на десятках тысяч баннеров
- [x] **bug_225** — Pool config дефолтный (5+10): на 10+ конкурентных
  запросах вылетит "QueuePool limit exceeded"
- [ ] **bug_226** — Dashboard `COUNT(*)` без WHERE по 5+ таблицам:
  на 100k+ записей запрос становится секундами
- [x] **bug_234** — PDF-handler не cap'ает `attempts`: poison message
  крутится в очереди вечно
- [x] **bug_235** — `ad_handler._periodic_flush()` не отменяется при
  shutdown → потеря батча событий в памяти
- [x] **bug_243** — `drop_type` отсутствует в 3 миграциях
  (`sexenum`, `ticketstatus`/`ticketpriority`, `outboxstatus`):
  повторный upgrade упадёт «type already exists»
- [x] **bug_244** — `app/database.py:get_db()` оборачивает `yield` в
  try/except OperationalError → ошибки внутри handler'а конвертируются
  в 503

### LOW (P2)

- [ ] **bug_215** — Classified price=0 без is_free флага (бизнес-смысл
  не определён)
- [x] **bug_216** — Файлы в classified add_images проверяются только
  на ownership самого classified, не на ownership файла (overlap с 212)
- [ ] **bug_217** — N+1 в `set_best_of_breed`: 5 раз вызывает
  `_resolve_animal_type` для одной операции
- [ ] **bug_218** — `litter.price_from > price_to` не валидируется
- [ ] **bug_219** — `/references/breeds?per_page=200` без rate-limit
- [ ] **bug_220** — `classified_image.position` без верхней границы
- [ ] **bug_227** — `pool_recycle=1800` может превышать PgBouncer
  `idle_in_transaction_session_timeout`
- [ ] **bug_228** — Нет GIN-индекса на `moderation_logs.extra` JSONB
- [ ] **bug_229** — Нет композитного индекса на `(is_best_in_show,
  show_entry_id)` для топ-репортов
- [ ] **bug_237** — Outbox publisher без backoff: спамит DB-запросами
  пока Rabbit лежит
- [ ] **bug_238** — Jinja template рендерится N раз для N подписчиков
  одного события (вместо 1 раз + рассылка)
- [ ] **bug_239** — Нет DLX (dead-letter exchange): `nack(requeue=False)`
  тихо теряет malformed-сообщения
- [ ] **bug_240** — `ad_handler._flush()` swallow'ит исключение и
  очищает буфер → потеря событий при сбое БД
- [ ] **bug_241** — `notification.mark_sent()` не проверяет rowcount
- [ ] **bug_242** — `datetime.utcnow()` (deprecated) в 3 местах
  `app/services/scheduler.py` (lines 119, 152, 226)
- [ ] **bug_245** — `RequestIdMiddleware` доверяет клиентскому
  `X-Request-ID` без валидации UUID
- [ ] **bug_246** — `progressive_ban` non-atomic check-then-zadd
- [ ] **bug_247** — `progressive_ban` fail-open при сбое Redis:
  rate-limit беззвучно отключается

---

## Детали

### bug_208 — IDOR в /shows/{show_id}/results/{result_id} (HIGH)

- **Файлы:** `app/routers/results.py:102-130`, `app/services/result.py`.
- **Что:** PUT-хендлер принимает `show_id` и `result_id` в path. Service
  проверяет `_can_modify_results` на уровне show, но НЕ проверяет, что
  `result.entry.show_id == show_id`. Атакующий-организатор показа Y
  может прислать `PUT /shows/X/results/<id_из_Y>` — если он не имеет
  доступа к X, но имеет к Y, проверка ACL не сработает корректно.
- **Фикс:** после загрузки result сделать `entry = await
  db.get(ShowEntry, result.show_entry_id); assert entry.show_id ==
  show_id`. Иначе 404 (не выдавать 403, чтобы не утечь существование).

### bug_209 — Race в каскаде BoB/BIG/BIS (HIGH)

- **Файл:** `app/services/result.py:271-290` (`set_best_of_breed`),
  `app/services/result.py:378-388` (`set_best_in_group`).
- **Что:** В bug_019 предыдущего ревью был добавлен каскадный сброс
  is_best_in_group/is_best_in_show при re-election BoB. Но
  одновременный вызов двумя секретарями на одну породу: оба читают
  старого BoB, оба пишут нового — один из reset'ов теряется, в БД
  остаются два BoB-флага и два BIS-флага.
- **Фикс:** `select(ShowResult).where(...).with_for_update()` при
  загрузке кандидатов на сброс. Делает критическую секцию
  сериализованной по этому breed_id.

### bug_210 — Classified status mass-assignable (HIGH)

- **Файл:** `app/routers/classifieds.py:154-174`,
  `app/services/classified.py:68-89` (`update_classified`).
- **Что:** `ClassifiedUpdate` включает `status` (см. `app/schemas/
  classified.py`). Service применяет через `setattr(obj, k, v)` без
  проверки разрешённых переходов. Состояния по докстрингу модели:
  `moderation → active`, `active → closed`, `closed → archived`. А
  через PUT клиент может «откатить» closed → active без модерации.
- **Фикс:** добавить `_validate_status_transition(old, new)` в сервис.
  Либо удалить `status` из `ClassifiedUpdate` — модерация уже идёт
  через `/admin/moderation/classifieds/{id}`.

### bug_211 — target_url пропускает javascript:-стиль (MEDIUM)

- **Файл:** `app/schemas/ad.py:73`.
- **Что:** Валидация regex `^https?://.+` пропускает URL вида
  `https://evil.com/redirect?to=javascript:alert(document.cookie)`.
  Сам по себе redirect через server-side не опасен — но если
  frontend подставит target_url в `<a href="…">` напрямую и где-то
  выше будет JS, обработающий клик через `window.location =
  this.href`, может полететь XSS.
- **Фикс:** strict-парсер через `urllib.parse.urlparse`; принимать
  только `https://`-схему и валидный hostname с ASCII-IDN или
  punycode. Frontend в любом случае должен encode'ить, но backend
  fail-closed.

### bug_212 / bug_216 — Чужие файлы в classified add_images (MEDIUM)

- **Файл:** `app/services/classified.py:92-130` (`add_images`).
- **Что:** Сервис проверяет ownership самого classified, но не
  проверяет, что `file_id` принадлежит автору (через
  `UploadedFile.uploaded_by == requester_id`). Атакующий, узнав
  чужой file_id (например, из публичного аватара), может прицепить
  его к своему объявлению.
- **Фикс:**
  ```python
  for img in images:
      f = await db.get(UploadedFile, img["file_id"])
      if f is None or (f.uploaded_by != requester_id and not is_admin):
          raise ValueError("file_forbidden")
  ```

### bug_213 — Public-эндпоинты без rate-limit (MEDIUM)

- **Файлы:** `app/routers/ads.py:212` (`record_event`),
  `app/routers/classifieds.py:63` (`search_classifieds`).
- **Что:** Оба эндпоинта open для анонимов и принимают unbounded
  поток. `record_event` дедуплицируется в Redis по 60s, но
  атакующий с ротацией IP/UA продавит дедуп. `search_classifieds`
  с 200-символьным `q` запускает PostgreSQL FTS — стоит CPU.
- **Фикс:** `check_rate_limit(request, limit=120, window=60, ...)`
  на `/ads/events`; `limit=30, window=60` на `/classifieds/search`.

### bug_214 — campaign.budget=0 (MEDIUM)

- **Файл:** `app/services/ad.py:273-280`.
- **Что:** Schema требует `budget > 0` при создании, но в БД можно
  попасть через прямой SQL/миграцию/UPDATE. Тогда
  `auto_complete_campaign_if_exhausted` со `spent >= budget` отдаст
  «exhausted» сразу. Но если `cost_per_impression=0`, то impressions
  записываются бесконечно без charge.
- **Фикс:** в начале `record_event` явный `if campaign.budget <= 0
  or campaign.status != active: return rejected`.

### bug_221/222/223 — Missing FK indexes (MEDIUM)

- **Файлы:** `app/models/show.py:284` (`ShowRing.judge_id`),
  `app/models/show.py:341` (`ShowEntry.handler_id`),
  `app/models/result.py:185` (`DogTitle.judge_id`).
- **Что:** PostgreSQL не создаёт индекс на FK автоматически. Любой
  `WHERE judge_id = ?` или JOIN по judge — full table scan. На тысячах
  записей замедление в 10-100×.
- **Фикс:** `mapped_column(..., ForeignKey(...), index=True)` +
  миграция `op.create_index('ix_show_rings_judge_id', 'show_rings',
  ['judge_id'])`.

### bug_224 — ORDER BY random() для подбора баннера (MEDIUM)

- **Файл:** `app/repositories/ad.py:155`.
- **Что:** При 10k+ баннеров `ORDER BY random()` оценивает random()
  для каждой строки, сортирует, берёт первый. O(n log n) → секунды
  на каждый `/ads/serve`.
- **Фикс (поэтапно):**
  1. Сначала фильтр по placement+active → подзапрос с LIMIT 100.
  2. Из этого подзапроса `ORDER BY random() LIMIT 1`.
  3. Долгосрочно: TABLESAMPLE BERNOULLI или weighted index в Redis.

### bug_225 — Pool config дефолтный (MEDIUM)

- **Файл:** `app/database.py:11-24`.
- **Что:** `pool_size` и `max_overflow` не заданы явно — дефолт
  SQLAlchemy 5+10. При >15 параллельных HTTP-запросах или WS-чатах
  (см. bug_205 фикс — теперь сессия per-сообщение, но всё равно)
  получим QueuePool overflow с длинной задержкой.
- **Фикс:** добавить в config `db_pool_size: int = 20`,
  `db_max_overflow: int = 10`, прокинуть в `create_async_engine`.

### bug_226 — Dashboard COUNT(*) без WHERE (MEDIUM)

- **Файл:** `app/repositories/analytics.py:32-42` (`DASHBOARD_SQL`).
- **Что:** `SELECT COUNT(*) FROM dogs`, `... FROM users` без условий.
  На 100k+ строк каждый COUNT = 1-2 сек. Дашборд тормозит на каждое
  открытие. Без кеширования не лучше.
- **Фикс:** кеш в Redis с TTL=5 мин на ключ `analytics:dashboard:v1`,
  отдавать `last_updated_at` клиенту. Параллельно — `EXPLAIN ANALYZE`
  чтобы понять, выручают ли индексы.

### bug_230 — Email idempotency (HIGH)

- **Файлы:** `worker/handlers/email_handler.py:22-42`,
  `worker/handlers/events_handler.py:92-113`.
- **Что:** Notification создаётся в events_handler ДО публикации в
  email_tasks. Если consumer крашится после mark_published, но до
  ack — Rabbit повторно доставит сообщение. events_handler создаст
  ВТОРУЮ Notification, опубликует ВТОРОЕ email_task. Юзер получит
  два письма, в БД две одинаковые строки.
- **Фикс:** добавить `message_id: uuid.UUID` в `EmailTaskMessage` —
  ИЗ outbox.id или явный uuid в публикуемом payload. В email_handler
  перед отправкой: `SELECT FROM notifications WHERE message_id =
  ?` — если есть, ack и выйти. UNIQUE-constraint на message_id
  защищает на уровне БД.

### bug_231 — Events handler race (HIGH)

- **Файл:** `worker/handlers/events_handler.py:92-113`.
- **Что:** create_notification commit'ит запись, потом publish в
  email_tasks. Если worker крашится между ними — Notification в БД
  висит pending, в Rabbit ничего нет. Без reconciliation письмо
  никогда не уйдёт.
- **Фикс:** transactional outbox и здесь. Один INSERT в
  `outbox_events` вместе с Notification внутри одной транзакции;
  outbox-publisher вытолкнет в email_tasks (или сразу запишет
  Notification со статусом sent после успешного publish — но это
  ломает архитектуру).

### bug_232 — Outbox batch commit (HIGH)

- **Файл:** `worker/handlers/outbox_handler.py:41-77`.
- **Что:** `dispatch_once()` берёт N pending, для каждого делает
  publish + `repo.mark_sent` без commit, и commit'ит один раз в
  конце. Если publish#5 кинул исключение — события #1..#4 уже
  помечены `sent` в Python-объектах, но commit не случился → при
  следующем цикле они опять выберутся как pending и поедут второй
  раз.
- **Фикс:** commit ПОСЛЕ КАЖДОГО успешного publish:
  ```python
  for event in events:
      try:
          await _publish(channel, event)
          await outbox_repo.mark_sent(db, event.id)
          await db.commit()
      except Exception as e:
          await db.rollback()
          await outbox_repo.mark_failed(db, event.id, str(e))
          await db.commit()
  ```

### bug_233 — task.mark_done() без WHERE status (HIGH)

- **Файлы:** `app/repositories/task.py:42-65`,
  `worker/handlers/document_handler.py:48-77`.
- **Что:** `claim_task` атомарно переводит pending→processing
  (защищено WHERE status='pending'). Но `mark_done` блиндно
  UPDATE'ит без проверки текущего статуса. Если в результате
  баги/гонки два воркера схватили одну задачу (теоретически
  возможно через split-brain в pg + connection pool), второй
  перезапишет результат первого.
- **Фикс:** `UPDATE tasks SET status='done', result=:result
  WHERE id=:id AND status='processing'` — rowcount=0 значит другая
  ситуация, логируем warning.

### bug_236 — Scheduler multi-instance (HIGH)

- **Файл:** `app/services/scheduler.py:61-87`.
- **Что:** APScheduler стартует в lifespan каждого FastAPI-инстанса.
  При replicas=3 в k8s/compose:
  - `requeue_stuck_tasks` ставит в outbox по 3 копии каждой stuck-задачи.
  - `archive_old_classifieds` пытается архивировать одни и те же — спам
    UPDATE'ов.
  - `cleanup_tokens` — три DELETE'а параллельно (норм, но шум в логах).
- **Фикс:** Redis-блокировка перед каждой задачей:
  ```python
  async with redis.lock(f"scheduler:{task_name}", timeout=300):
      await actual_task()
  ```
  Альтернатива: отдельный scheduler-контейнер с `scheduler_enabled=
  true`, у воркеров и API — false. Это чище для prod.

### bug_243 — Missing drop_type в миграциях (MEDIUM)

- **Файлы:**
  - `migrations/versions/55e42cedc4af_stage_04_kennels_dogs_files.py`
    (`sexenum`),
  - `migrations/versions/b713639e179a_stage_11_support.py`
    (`ticketstatus`, `ticketpriority`),
  - `migrations/versions/ea996647ff46_outbox_events.py`
    (`outboxstatus`).
- **Что:** В downgrade()'ах нет `sa.Enum(name='...').drop(
  op.get_bind(), checkfirst=True)`. На повторном upgrade Alembic
  упадёт «type already exists». Уже исправлено для большинства
  миграций — эти три пропущены.
- **Фикс:** добавить вызов `.drop()` в downgrade каждой указанной
  миграции (паттерн см. `9357e3441b8e_stage_05_litters_classifieds.py:
  122`).

### bug_244 — get_db() ловит OperationalError на yield (MEDIUM)

- **Файл:** `app/database.py:38`.
- **Что:**
  ```python
  try:
      async with async_session_factory() as session:
          yield session
  except (OperationalError, InterfaceError, OSError):
      raise HTTPException(503, ...)
  ```
  `yield` — точка передачи управления handler'у. Любая
  OperationalError, поднятая ВНУТРИ handler'а (например, нарушение
  unique constraint мог бы дойти как IntegrityError, но
  query-timeout = OperationalError), будет завернута в 503. Это
  скрывает реальную ошибку.
- **Фикс:** ловить ошибки только при получении соединения:
  ```python
  try:
      session = async_session_factory()
      await session.execute(text("SELECT 1"))  # ping
  except (OperationalError, ...):
      raise HTTPException(503, ...)
  async with session:
      yield session
  ```
  Либо вообще убрать обёртку — пусть ErrorHandlerMiddleware ловит
  и логирует.

---

## Низкоприоритетные (но фиксить)

### bug_237 — Outbox publisher без backoff (LOW)

- **Файл:** `worker/handlers/outbox_handler.py:109-138`.
- **Что:** При недоступности Rabbit `_publish` падает на
  declare_exchange. Loop ловит и спит фиксированный poll_interval.
  В логах за час набегает тысяча однотипных warning'ов.
- **Фикс:** экспоненциальный backoff после N последовательных
  ошибок (2s → 4s → 8s → max 60s); сброс при первом успехе.

### bug_238 — Re-render Jinja per subscriber (LOW)

- **Файл:** `worker/handlers/events_handler.py:89`.
- **Что:** На событие «новый помёт» подписаны 1000 пользователей —
  render_email вызывается 1000 раз с одним и тем же context (имя
  питомника, breed, и т.д. одинаковые). CPU greedy.
- **Фикс:** render один раз в events_handler, передать готовый
  html/text в email_task payload. Email_handler только шлёт SMTP.

### bug_239 — Нет DLX (LOW)

- **Файл:** `worker/main.py` (consumer setup),
  `app/services/rabbit.py` (publish/declare).
- **Что:** При `nack(requeue=False)` сообщение тихо удаляется
  если нет DLX. Никакой видимости в malformed payload'ы.
- **Фикс:** declare DLX на каждый exchange, dead-letter queue
  с retention, ops-алерт на размер.

### bug_240 — ad_handler._flush() swallow (LOW)

- **Файл:** `worker/handlers/ad_handler.py:166-174`.
- **Что:** При сбое commit'а batch теряется (буфер очищен ДО
  commit). Биллинг рекламы недосчитается событий.
- **Фикс:** очищать буфер ПОСЛЕ commit; на retry оставить ту же
  партию и инкрементировать счётчик попыток.

### bug_241 — mark_sent без rowcount-check (LOW)

- **Файл:** `app/repositories/notification.py:175-182`.
- **Что:** UPDATE notifications SET status='sent' WHERE id=?
  без проверки `result.rowcount`. При неверном UUID молча 0
  изменений, лог не пишется.
- **Фикс:** `if result.rowcount == 0: raise ValueError("not_found")`.

### bug_242 — datetime.utcnow() deprecated (LOW)

- **Файл:** `app/services/scheduler.py:119, 152, 226`.
- **Что:** Python 3.12+ помечает `datetime.utcnow()` deprecated;
  возвращает naive datetime — сравнение с tz-aware колонкой БД
  ломается на границе DST или просто как warning.
- **Фикс:** `datetime.now(timezone.utc)` — replace_all.

### bug_245 — X-Request-ID trusted (LOW)

- **Файл:** `app/middleware/request_id.py:8`.
- **Что:** Клиент может прислать `X-Request-ID:
  <injected log content>` и засрать логи. Также — если он
  валиден UUID — может предсказывать request_id, что в редких
  случаях помогает в атаках на cache.
- **Фикс:** validate как UUID; если не валиден — генерировать
  свой.

### bug_246 — progressive_ban check-then-write race (LOW)

- **Файл:** `app/middleware/progressive_ban.py:50-77`.
- **Что:** `zcard + zadd` неатомарны. Два параллельных запроса
  оба видят count < limit, оба добавляются в sorted set, оба
  проходят — лимит пробит на 1.
- **Фикс:** Lua script (`EVAL`) или `MULTI/EXEC`, что выполнит
  всё атомарно.

### bug_247 — progressive_ban fail-open (LOW, но это политика)

- **Файл:** `app/middleware/progressive_ban.py:81`.
- **Что:** `except Exception: logger.warning(...)` без re-raise.
  Если Redis недоступен — rate-limit беззвучно отключается. Под
  атакой на Redis = одновременно атака на login (нет защиты).
- **Фикс:** опциональный флаг `settings.rate_limit_fail_closed:
  bool = False`. Когда True — Redis-сбой → 503 на rate-limit'нутые
  эндпоинты.

---

## Положительные наблюдения (не трогать)

- **State machine `app/services/show_rules.py`** — централизованные
  правила переходов, легко рассуждать.
- **Transactional outbox для шоу/помётов** — `publish_event(..., db=
  db)` гарантирует «событие → ровно тогда же, когда основной commit».
- **Atomic `try_charge_campaign`** в ads/service — условный UPDATE
  без overspending.
- **Mass-assignment защита** — Pydantic-схемы фильтруют входящий JSON
  перед `setattr(obj, k, v)` в сервисах (bug_210 — исключение,
  потому что в схему включён сам status).
- **Refresh-token rotation + reuse detection** (`app/services/auth.py:
  137-190`) — уже видели в предыдущем ревью.
- **FTS-параметризация** — `text("... :query ...")` без f-string.
- **Pool pre-ping + recycle** — `app/database.py:14-23` (мелкие
  замечания см. bug_225, 227).

---

## Приоритизация фиксов

**P0 (одна сессия, ~2 часа):**
1. bug_233 (task.mark_done WHERE) — 5 мин.
2. bug_208 (IDOR results) — 10 мин.
3. bug_210 (classified status mass-assign) — 10 мин.
4. bug_232 (outbox per-event commit) — 20 мин.
5. bug_243 (drop_type в 3 миграциях) — 15 мин.
6. bug_209 (FOR UPDATE в каскаде BoB) — 30 мин.
7. bug_236 (scheduler Redis lock) — 30 мин.

**P1 (вторая сессия, ~2 часа):**
- bug_230/231 (email idempotency + outbox для notifications) — крупно,
  отдельный PR.
- bug_221/222/223 (FK индексы + миграция) — 20 мин включая alembic
  revision.
- bug_212/216 (file ownership) — 15 мин.
- bug_213 (rate limit на ads/events и classifieds/search) — 15 мин.
- bug_244 (get_db cleanup) — 15 мин.

**P2 (планомерно):**
- Остальные performance + dx-fixes (bug_217, 218, 219, 220, 226,
  227, 228, 229, 235, 237-242, 245-247).

## Чего этот аудит НЕ покрывает

- Полная нагрузочная проба с `EXPLAIN ANALYZE` на синтетических
  данных (нужно для подтверждения bug_224, 226, 229).
- WebSocket-нагрузка с тысячами одновременных соединений
  (bug_205 fix теоретически устраняет утечку — нужен load-test).
- Безопасность фронта (CSP, XSS контекст в JSX/Vue templates) —
  бэкенд возвращает JSON, проверка лежит на клиенте.
- Deploy-конфигурация (CORS allow_origins, ALLOWED_HOSTS,
  HSTS) — описано в `docs/plans/future/technical-debt.md`.
