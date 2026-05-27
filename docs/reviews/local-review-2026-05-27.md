# Локальный ревью проекта — 2026-05-27

**Источник:** ручной аудит после трёх неуспешных прогонов `/ultrareview`
(один частичный результат был виден в скриншоте `Снимок экрана
2026-05-27 203415.png`). Перечисленные ниже находки подтверждены
прямым чтением кода — это не реконструкция скриншота, а валидация
каждой гипотезы по файлам и номерам строк.

**Скоуп:** код, появившийся в текущей итерации проекта (этапы 4–15) —
тот, что не лежит в `git log` главной ветки больше суток. Уже
исправленные баги из `ultrareview-fixes.md` (bug_001, bug_009, bug_011,
bug_012, bug_018, bug_019) повторно не перечисляю.

## Статусы

- [x] **bug_201** — IDOR: `/tasks/{id}/download` без ACL (HIGH)
- [x] **bug_202** — Content-Disposition header injection через
  `original_filename` (HIGH)
- [ ] **bug_203** — `PUT /users/me` смена email без верификации и без
  отзыва сессий (HIGH)
- [x] **bug_204** — Admin может self-block / отозвать собственную роль
  admin (HIGH)
- [ ] **bug_205** — WS support держит `AsyncSession` на всё время жизни
  соединения, исчерпывает пул (MEDIUM)
- [ ] **bug_206** — WS support не пере-проверяет доступ / is_active
  после первоначальной аутентификации (LOW)
- [ ] **bug_207** — `app/routers/documents.py:_publish_task` пишет
  задачу в БД, но при неудачном publish в RabbitMQ оставляет её в
  `pending` без явного механизма retry (LOW, документ-долг)

## Детали

### bug_201 — IDOR на скачивании результата задачи (HIGH, security)

- **Файл:** `app/routers/tasks.py:119-172`.
- **Суть:** хендлер `download_task_result` принимает `user:
  User = Depends(get_current_user)` с явным комментарием в коде:
  `# noqa: ARG001 — пока без ACL`. После проверки наличия задачи и
  статуса `done` файл отдаётся ЛЮБОМУ авторизованному пользователю —
  организатору, другому организатору, обычному заводчику. Получив
  один UUID задачи (например, из логов nginx или утечки в API),
  пользователь B скачает каталог/диплом, сгенерированный для
  пользователя A.
- **Сценарий:** A создаёт каталог выставки → `task_id` уходит в JS-логи
  / sentry → B забирает PDF. Каталог содержит персональные данные
  владельцев собак (телефоны, адреса питомников).
- **Фикс:**
  ```python
  if task.created_by != user.id and not _is_admin(user):
      raise HTTPException(403, "forbidden")
  ```
  плюс fail-closed: если `task.created_by is None` (исторические
  задачи без автора) — тоже 403, не давать доступ.

### bug_202 — Header injection через original_filename (HIGH, security)

- **Файл:** `app/routers/tasks.py:168-170`, `app/routers/files.py:80`.
- **Суть:** `f'attachment; filename="{db_file.original_filename}"'` —
  интерполяция без экранирования. Если файл загружен с именем
  содержащим `\r\n` или `"`, в HTTP-ответе появляются произвольные
  заголовки. Сценарий:
  1. Атакующий загружает файл с именем
     `evil.pdf"\r\nSet-Cookie: session=...; Path=/\r\n\r\n<html>`.
  2. Жертва скачивает файл → браузер видит инжектированный `Set-Cookie`
     (session fixation) или `Content-Security-Policy: ...` (отключение
     защиты).
- **Дополнительно:** `original_filename` приходит в `upload_file` от
  клиента (`upload.filename or "file"`) — никакой валидации не делается
  (multipart-парсер Starlette формат разрешает почти любые байты).
- **Фикс:** RFC 6266-compliant сериализация. Минимально:
  ```python
  from urllib.parse import quote
  safe = quote(db_file.original_filename or "file", safe="")
  headers={
      "Content-Disposition":
          f"attachment; filename*=UTF-8''{safe}",
  }
  ```
  Идеально — переключиться на `fastapi.responses.FileResponse`/
  `StreamingResponse` с явным `filename=` параметром (Starlette сам
  процитирует).

### bug_203 — Смена email без верификации/инвалидации (HIGH, security)

- **Файл:** `app/routers/users.py:24-45`.
- **Суть:** при `PUT /users/me` со сменой email хендлер только сбрасывает
  `is_email_verified=False`. НЕ выполняется:
  1. Генерация нового `email_verification_tokens` и отправка письма
     на НОВЫЙ адрес.
  2. Отзыв активных refresh-токенов пользователя.
  3. Опциональное подтверждение паролем (re-auth) — стандарт для
     "sensitive operations".
- **Атака:** A заходит в чужую сессию (краденый access-токен / открытая
  сессия в браузере). Меняет email на свой через PUT /users/me — без
  верификации, без re-auth. Далее на любом эндпоинте смены пароля
  (когда будет добавлен — этап 14 ещё не покрыт) пускает «forgot
  password» на свой email → захватывает аккаунт навсегда.
- **Фикс:**
  ```python
  if "email" in fields and fields["email"] != current_user.email:
      raw, h = generate_verification_token()
      await user_repo.create_email_verification_token(
          db, current_user.id, h,
          datetime.now(timezone.utc) + timedelta(hours=24),
      )
      # TODO: отправить письмо с raw на НОВЫЙ адрес (worker.email).
      # Email в БД меняем только ПОСЛЕ подтверждения нового адреса —
      # текущая запись сохраняется до verify-email-change.
      fields.pop("email")
      fields["is_email_verified"] = False  # на случай других правок
      # Дополнительно: отозвать все refresh-токены.
      await user_repo.revoke_all_refresh_tokens_for_user(
          db, current_user.id
      )
  ```
  Сам апдейт `email` колонки лучше делать в отдельном эндпоинте
  `POST /users/me/email-change/confirm?token=...`, чтобы не было
  состояния «email уже сменили, верификация ещё не пришла».

### bug_204 — Admin self-block / self-demote (HIGH, correctness)

- **Файлы:** `app/services/moderation.py:114-160` (`block_user`),
  `app/services/moderation.py:163-204` (`update_user_role`),
  роутер: `app/routers/admin/moderation.py:218-251`.
- **Суть:** ни в одном из двух эндпоинтов нет проверки, что
  `actor.id != target_user.id`. Сценарии:
  1. `PUT /admin/users/<свой_id>/block` с `is_active=false` → админ
     блокирует сам себя, теряет доступ. Восстановить можно только
     через прямой SQL (нет другого админа в системе).
  2. `PUT /admin/users/<свой_id>/role` с `grant=false, role=admin` →
     админ снимает с себя роль admin. Если он был единственным
     админом — система остаётся без admin'ов вовсе.
- **Фикс в `block_user`:**
  ```python
  if user_id == actor_id and is_active is False:
      raise ValueError("cannot_block_self")
  ```
- **Фикс в `update_user_role`:** запретить снятие admin-роли с
  себя; дополнительно для критичности — запретить снятие admin-роли
  с **последнего** админа (SELECT COUNT(*) FROM user_roles
  WHERE role='admin' — если 1 и `not grant`, отказать). Это
  защищает не только от self-demote, но и от случайного "удалю
  Васю как admin'а" когда Вася единственный остался.

### bug_205 — DB connection leak в support_ws (MEDIUM, reliability)

- **Файл:** `app/routers/support.py:257-358`.
- **Суть:** строка 274 — `async with async_session_factory() as db:` —
  оборачивает ВЕСЬ цикл жизни WS, включая блокирующее ожидание
  `await websocket.receive_json()`. Это значит: пользователь
  открыл чат поддержки и ушёл пить кофе → соединение из пула
  PostgreSQL занято этим WS до тех пор, пока пользователь не закроет
  вкладку или таймаут не сработает. Несколько идл'ящих чатов
  выедают пул (`pool_size=5` по умолчанию, `max_overflow=10` в
  `database.py`) — следующие HTTP-запросы будут ждать.
- **Симптомы при нагрузке:** 503 от health-эндпоинта, "QueuePool
  limit overflow" в логах, общая деградация API.
- **Фикс (минимальный):** не оборачивать весь loop в context.
  AUTH/ACCESS-фазу — да, под одним коротким `async with`.
  Внутри цикла на каждое сообщение открывать свою сессию:
  ```python
  await websocket.accept()
  async with async_session_factory() as db:
      user = await _authenticate_ws(db, first["token"])
      # ... ACCESS CHECK + mark_read ...
  # ВЫЙТИ из async with — освободить connection.

  try:
      while True:
          frame = await websocket.receive_json()
          # ... валидация frame ...
          async with async_session_factory() as db:
              msg = await repo.add_message(db, ...)
          await ws_manager.publish(ticket_id, ...)
  except WebSocketDisconnect:
      ...
  ```
- **Альтернатива:** оставить как есть, но завести отдельный
  pool для WS (`asyncpg.create_pool` с `max_size=200`). Сложнее
  и не покрывает корневую причину.

### bug_206 — WS не пере-проверяет доступ (LOW, security)

- **Файл:** `app/routers/support.py:306-358`.
- **Суть:** `can_access_ticket` и `user.is_active` проверены ОДИН раз —
  на handshake. Если в течение долго живущей WS-сессии пользователю
  заблокировали аккаунт (`block_user`), сняли роль operator или
  re-assigned тикет — он всё равно может слать сообщения до закрытия
  сокета.
- **Фикс:** периодически (раз в N секунд / каждые M сообщений)
  перечитывать `user.is_active` и `can_access_ticket(ticket, user)`.
  Самый простой — на каждое входящее `frame.type == 'message'`
  делать lightweight refresh через `db.refresh(user)`.
- **Smell:** связан с bug_205 (long-lived session) — если пофиксить
  bug_205 с per-message сессией, refresh user сам собой ложится в
  блок «открыли новую сессию → прочитали пользователя».

### bug_207 — Утрата задачи при сбое RabbitMQ (LOW, reliability)

- **Файл:** `app/routers/documents.py:69-101`.
- **Суть:** `_publish_task` пишет Task в БД, потом пытается
  опубликовать в RabbitMQ. Если publish упал — лог
  `"Failed to publish task ... to RabbitMQ"` и всё; в БД остаётся
  pending-задача без шансов исполниться. Комментарий в коде честно
  обещает admin-эндпоинт «будет на этапе 14», но его всё ещё нет.
- **Это не security**, это надёжность — записал в
  `docs/plans/future/technical-debt.md` если ещё не записано.
  Долгосрочное решение — outbox + reconciliation worker (он у нас
  частично уже есть — `outbox_events`).

## Положительные наблюдения

(Это к тому, что **не** надо переделывать — чтобы не сломать.)

- FTS-поиск (`app/repositories/classified.py:178-252`) использует
  параметризованный `text()` с `:query` bind, инъекции нет.
- Timing-attack защита в `login_user` через `dummy_verify_password()`
  и одинаковый ответ ошибки.
- Refresh-token rotation + reuse-detection реализованы в
  `services/auth.py:137-190`.
- `decode_access_token` явно требует `require_exp` и `require_sub`.
- Idempotency-middleware уже включает identity в кеш-ключ (фикс
  bug_018 из предыдущего ультра-ревью).
- Sanitization middleware теперь пишет `_body` (фикс bug_001).
- ProxyHeadersMiddleware валидирует XFF как IP (фикс bug_012).

## Приоритеты исправления

| Приоритет | Bug | Время | Сложность |
|-----------|-----|-------|-----------|
| P0 | bug_201 (IDOR) | 10 мин | trivial |
| P0 | bug_202 (header inj) | 15 мин | trivial |
| P0 | bug_204 (admin self) | 15 мин | trivial |
| P1 | bug_203 (email change) | 1-2 ч | требует worker для отправки |
| P1 | bug_205 (WS leak) | 30 мин | требует тест нагрузкой |
| P2 | bug_206 (WS re-check) | 15 мин (lazy) | trivial |
| P3 | bug_207 (RabbitMQ retry) | уже в долге | — |

P0 фиксим в одном PR, рядом. P1 — два отдельных PR с
регрессионными тестами. P2/P3 — отдельно или вместе с
интеграционными тестами (`docs/plans/future/technical-debt.md`).

## Что не покрыто этим ревью

- Performance / N+1 / индексы (нужно с `EXPLAIN ANALYZE` на реальных
  данных — на этапе seed недостаточно).
- Race conditions при concurrent публикации результатов (только
  set_best_of_breed/group/show покрыто bug_019 предыдущего round'а).
- Worker-side (handlers/, scheduler/) — отдельный пасс нужен.
- Migrations: downgrade-логика для PG enum не везде явно дропает
  тип (`DROP TYPE`), но это не critical, проверим перед prod.
