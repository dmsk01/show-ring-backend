# Ultrareview fixes — чек-лист

Источник: `/ultrareview` на main, прогон 2026-05-27. Отчёт нашёл
5 normal + 1 nit. Все 5 normal — security/correctness, 4 из 5 в
коде, добавленном текущей сессией. Чек-лист ниже отмечается по мере
закрытия каждого пункта.

## Статусы

- [x] **bug_001** — Sanitization middleware silently bypassed
- [x] **bug_018** — Idempotency cache key missing user identity
- [x] **bug_011** — Jinja autoescape disabled for `.html.j2`
- [x] **bug_012** — ProxyHeadersMiddleware: XFF value not validated as IP
- [x] **bug_019** — set_best_of_breed/set_best_in_group leave stale BIG/BIS flags
- [x] **bug_009** — `/users/admin/list` returns single user, not list

## Детали

### bug_001 — Sanitization bypass (normal, security)

- **Файл:** `app/middleware/sanitization.py:63-66`,
  `app/middleware/idempotency.py:147` (тот же шаблон).
- **Суть:** `request._receive = receive` после `await request.body()` —
  no-op. Starlette `_CachedRequest.wrapped_receive` возвращает
  `request._body` перед обращением к `_receive`. SENSITIVE_FIELDS
  и весь bleach.clean в санитизации не доходят до handler'а.
- **Фикс:** `request._body = new_body` рядом с `_receive`
  override. Тот же фикс в `idempotency.py` (там без вреда — body
  не меняется, но устраняем класс ошибки).
- **Регрессионный тест:** integration с `<script>` в JSON-поле,
  проверить что handler получает очищенное значение.

### bug_018 — Idempotency cross-user response leak (normal, security)

- **Файл:** `app/middleware/idempotency.py:51-63`.
- **Суть:** Кеш-ключ из `(method, path, key, body_hash)` без
  identity. На cache-hit middleware короткозамыкается ДО
  `Depends(get_current_user)`, поэтому user B (или анонимный
  replay) получает ответ user A с его resource id/owner ссылками.
  Плюс auth bypass для replay перехваченного ключа+тела.
- **Фикс:** `identity = sha256(Authorization header)` или fallback
  на `request.client.host` для анонимов; добавить identity в
  `_cache_key` и `_lock_key`.
- **Долгосрочно:** перенести idempotency-логику в Depends-уровень
  ПОСЛЕ `get_current_user`, чтобы identity была явно
  authenticated. Сейчас минимальный фикс.

### bug_011 — Jinja autoescape inactive (normal, security)

- **Файл:** `app/services/email.py:40-46`.
- **Суть:** `select_autoescape(default_for_string=False, default=False)`
  без `enabled_extensions` смотрит дефолтный список
  `('html','htm','xml')`. Шаблоны `*.html.j2` имеют trailing
  `.j2`, не матчатся, autoescape = False. Organizer/breeder-
  контролируемые поля (show_name, kennel_name, dog_name) идут
  в HTML-письма подписчикам сырыми → HTML-инъекция, phishing.
- **Фикс:** `enabled_extensions=('html','htm','xml','j2')` или
  `autoescape=True` (все шаблоны — HTML, escape unconditional
  безопасен).

### bug_012 — XFF not validated as IP (normal, security)

- **Файл:** `app/middleware/proxy_headers.py:73-80`.
- **Суть:** `xff.split(",")[0].strip()` пишется в
  `scope['client']` без проверки `ipaddress.ip_address`.
  Empty XFF / nginx-append (`proxy_add_x_forwarded_for`
  appends при misconfig) даёт client-controlled значение.
  rate-limit (`progressive_ban.rate:{ip}:{endpoint}`) и
  ad-fraud dedup (`ad_dedup:{banner}:{ip}:...`) обходятся
  ротацией XFF.
- **Фикс:** `try: ipaddress.ip_address(real_ip); except
  ValueError: fallback на original client`. Симметрия с
  `_is_trusted_peer` (там валидация уже есть).

### bug_019 — Best flags cascade (normal, correctness)

- **Файл:** `app/services/result.py:271-279, 367, 422` и
  `app/repositories/result.py:120-138`.
- **Суть:** При re-election BOB ex-winner сохраняет
  `is_best_in_group=True` и `is_best_in_show=True`.
  `set_best_of_breed` сбрасывает только {bob, male, female,
  junior, veteran}. `set_best_in_group` фильтрует через
  `list_results_by_group(... WHERE is_best_of_breed=True)` —
  ex-BOB (теперь `is_best_of_breed=False`) не попадает в reset.
  Инвариант BIS ⊆ BIG ⊆ BOB ломается → analytics over-count,
  PDF-каталог с двумя BIG/BIS.
- **Фикс:** в `set_best_of_breed` дополнительно сбросить
  `is_best_in_group` и `is_best_in_show` на возвращённых
  результатах; в `set_best_in_group` — также сбросить
  `is_best_in_show` на prev BIG-winner'ах. Это закрывает
  каскад.

### bug_009 — `/users/admin/list` misleading (nit)

- **Файл:** `app/routers/users.py:47-56`.
- **Суть:** Эндпоинт назван list_users_admin, summary
  «Список пользователей (admin)», но возвращает один
  `UserResponse` (профиль самого вызывающего admin'а).
  Реальный список — `GET /admin/users` в
  `routers/admin/moderation.py` с пагинацией и фильтрами.
  Эндпоинт misleading + redundant.
- **Фикс:** удалить endpoint целиком.

## Что осталось после фиксов

- Integration-тест для bug_001 (требует test-DB фикстур из
  technical-debt.md). Минимум — unit-тест через мок
  `BaseHTTPMiddleware._CachedRequest` или прямой вызов
  `SanitizationMiddleware.dispatch` с mocked request.
- Перенос idempotency на Depends-уровень после auth
  (долгосрочно для bug_018).
