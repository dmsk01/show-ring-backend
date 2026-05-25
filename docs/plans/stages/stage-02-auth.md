# Этап 2: Пользователи и авторизация

### Цель

Реализовать регистрацию, аутентификацию (JWT), мульти-ролевую модель, progressive rate limiting, email verification и базовые middleware.

### Что появляется в проекте

- Регистрация пользователя (email + пароль) с валидацией пароля
- Email verification — подтверждение почты по ссылке с токеном
- Логин → access token (короткоживущий) + refresh token (хранится в БД)
- Мульти-ролевая модель: user_roles (many-to-many), один пользователь = несколько ролей
- Роли: admin, organizer, breeder, judge, buyer
- Защита эндпоинтов по ролям (`Depends(require_any_role("admin", "organizer"))`)
- Progressive rate limiting через Redis (429 + Retry-After с экспоненциальным ростом)
- Middleware: request ID, sanitization, error handler
- Эндпоинт профиля `GET /users/me`

### Модель данных

**Таблица `users`:**
- password_hash (bcrypt)
- is_active, is_email_verified
- avatar_file_id (пока NULL — файлы будут в этапе 4)

> Поле `role` ENUM **убрано из users**. Роли хранятся в `user_roles`.

**Таблица `user_roles`:**
- user_id (FK → users)
- role (ENUM: admin, organizer, breeder, judge, buyer)
- granted_at, granted_by
- UNIQUE(user_id, role)

**Таблица `refresh_tokens`:**
- user_id, token_hash (SHA-256), expires_at, is_revoked
- Позволяет отзывать токены (logout, смена пароля, блокировка)

**Таблица `email_verification_tokens`:**
- user_id, token_hash, expires_at (24ч), used_at

### API эндпоинты

| Метод | Путь | Описание | Доступ |
|-------|------|----------|--------|
| POST | `/auth/register` | Регистрация → отправка verification email | Public |
| POST | `/auth/verify-email` | Подтверждение email по токену | Public |
| POST | `/auth/login` | Логин (JSON) → JWT (access + refresh) | Public |
| POST | `/auth/token` | OAuth2 form login (для Swagger Authorize) | Public |
| POST | `/auth/refresh` | Обновить access + refresh (rotation) | Public с refresh-токеном |
| POST | `/auth/logout` | Отозвать refresh token | Public с refresh-токеном |
| GET | `/users/me` | Свой профиль + email + роли | Authenticated |
| PUT | `/users/me` | Обновить профиль | Authenticated |
| GET | `/users/{id}` | Публичный профиль (БЕЗ email) | Public |

### Progressive Rate Limiting

Реализация через Redis middleware. Алгоритм:

```python
# Ключ: f"rate:{ip}:{endpoint}"
# Redis хранит: sorted set с timestamps запросов

async def check_rate_limit(ip: str, endpoint: str, limit: int, window: int):
    key = f"rate:{ip}:{endpoint}"
    ban_key = f"ban:{ip}:{endpoint}"
    
    # 1. Проверить, не забанен ли
    ban_ttl = await redis.ttl(ban_key)
    if ban_ttl > 0:
        raise HTTPException(429, headers={"Retry-After": str(ban_ttl)})
    
    # 2. Подсчитать запросы в окне
    now = time.time()
    await redis.zremrangebyscore(key, 0, now - window)
    count = await redis.zcard(key)
    
    if count >= limit:
        # 3. Экспоненциальный бан
        violations_key = f"violations:{ip}:{endpoint}"
        violations = await redis.incr(violations_key)
        await redis.expire(violations_key, 3600)  # сброс через 1 час
        ban_seconds = min(2 ** violations, 3600)
        await redis.setex(ban_key, ban_seconds, "1")
        raise HTTPException(429, headers={"Retry-After": str(ban_seconds)})
    
    # 4. Записать запрос
    await redis.zadd(key, {str(now): now})
    await redis.expire(key, window)
```

**Лимиты:**
| Эндпоинт | Лимит | Окно | Ключ |
|----------|-------|------|------|
| `POST /auth/login` | 5 | 60 сек | IP |
| `POST /auth/register` | 3 | 3600 сек | IP |
| `POST /auth/verify-email` | 10 | 60 сек | IP |
| Общий API | 60 | 60 сек | user_id или IP |

### Password Policy

```python
def validate_password(password: str) -> None:
    if len(password) < 8:
        raise ValueError("Минимум 8 символов")
    if len(password) > 128:
        raise ValueError("Максимум 128 символов")
```

> Начинаем с минимума (длина). Сложные правила (цифры, спецсимволы) добавляются позже при необходимости.

### Защита от user enumeration и timing attacks

- **`/auth/register`** возвращает **одинаковое** сообщение независимо от того,
  существует email или нет: `{"message": "Проверьте email для подтверждения"}`.
  Никаких 400 «email уже занят». При race-condition на UNIQUE-constraint
  сервис ловит `IntegrityError` и тоже возвращает успех.
- **`/auth/login`** при отсутствии пользователя выполняет dummy bcrypt
  verify (`utils.security.dummy_verify_password`), чтобы время ответа было
  сопоставимо с реальной проверкой пароля. Сообщение ошибки одно и то же
  для «нет такого юзера» и «неверный пароль» — `401 Неверный email или пароль`.
- **`/auth/login` и `/auth/refresh`** проверяют `user.is_active` — забаненный
  пользователь не получит токенов.

### Sanitization Middleware — список исключений

Глобальный `SanitizationMiddleware` пропускает чувствительные поля без
изменений (`password`, `current_password`, `new_password`, `old_password`,
`token`, `access_token`, `refresh_token`, `hashed_password`, `api_key`,
`secret`). Иначе bleach изменяет значение (`"ab<x>cd"` → `"abcd"`), и хеш
при регистрации не совпадает с хешем при логине.

### Атомарность одноразовых токенов

- **email verification:** `mark_email_token_used` — атомарный
  `UPDATE ... WHERE used_at IS NULL` и проверка `rowcount`. Без этого
  два параллельных запроса с одним токеном могли пройти оба.
- **refresh token revoke:** `revoke_refresh_token` —
  `UPDATE ... WHERE is_revoked = FALSE` с проверкой `rowcount`,
  чтобы отличать «не нашли» от «уже отозван».

### Refresh Token Rotation + Reuse Detection

- При **успешном `/auth/refresh`** старый refresh-токен **атомарно**
  отзывается (`UPDATE ... WHERE is_revoked = FALSE`), и пользователю
  выдаётся **новый** access + **новый** refresh (формат — `TokenResponse`).
- **Reuse detection (defense-in-depth):** если `/auth/refresh` получил
  токен, который **существует в БД, но уже отозван** — это потенциальная
  утечка refresh (атакующий повторно использует украденный токен).
  В этом случае:
  - отзываются **ВСЕ** активные refresh этого user_id;
  - событие `refresh_token_reuse` пишется в `app.security`;
  - пользователю возвращается 401, ему нужен заново `/auth/login`.
- Семантика **`/auth/logout`** не меняется: вход тот же `RefreshRequest`,
  атомарный revoke по `token_hash`.
- **Trade-off:** при сетевых сбоях клиент может потерять новый refresh.
  Принято: клиент обязан атомарно сохранять новую пару, иначе делает
  заново `/auth/login`. Без сложных схем grace-window.

### Security-логирование

- Отдельный логгер `app.security` для security-relevant событий.
  Никогда не пишет пароли/raw токены — только `email`/`user_id` и тип.
- Сейчас пишутся события:
  - `login_failed reason=no_user|bad_password`
  - `login_blocked`
  - `register_existing_email`, `register_race_collision`
  - `email_verify_race`
  - `refresh_token_reuse`
- На стейдже 14 (production) этот логгер направляется в отдельный sink
  (отдельный файл / SIEM), чтобы security-события можно было отдельно
  алертить и хранить дольше application-логов.

### Публичная схема `/users/{id}`

- Возвращает `PublicUserResponse` — **без `email`** и **без
  `is_email_verified`**, иначе любой неавторизованный клиент мог собирать
  email'ы перебором UUID.
- `/users/me` (за `Depends(get_current_user)`) по-прежнему возвращает
  `UserResponse` с email — сам себе юзер может видеть свои PII.

### OAuth2 form-эндпоинт для Swagger

- `OAuth2PasswordBearer(tokenUrl=...)` указывает на эндпоинт, который
  по спецификации **обязан** принимать form-data (`username`, `password`).
- Решение: оставить `/auth/login` JSON-ным (контракт прикладных клиентов
  не меняется) и добавить отдельный `/auth/token`, который принимает
  `OAuth2PasswordRequestForm` и возвращает тот же `TokenResponse`.
- `tokenUrl` в `dependencies.py` переключён на `/auth/token` — кнопка
  «Authorize» в Swagger UI теперь работает.

### Заметка про CORS (стейдж 2 → стейдж 14)

- `CORSMiddleware` подключается в `app/main.py` только если
  `CORS_ALLOW_ORIGINS` непустой.
- На стейдже 14 (production) сюда добавляется список реальных фронт-доменов
  (фронт + админка). До этого `[]` — никакой обёртки CORS, никакого `*`.

### Email Verification Flow

```
1. POST /auth/register → создаёт user (is_email_verified=False)
                        → генерирует токен (32 байта, urandom)
                        → сохраняет SHA-256 хеш в email_verification_tokens
                        → публикует задачу в RabbitMQ (отправка email)
                        → возвращает {message: "Проверьте email"}

2. Пользователь получает email со ссылкой: /auth/verify-email?token=...

3. POST /auth/verify-email → проверяет хеш токена в БД
                            → проверяет срок (24 часа)
                            → ставит is_email_verified=True
                            → помечает токен как использованный

Примечание: на этапе 2 email отправляется в лог (нет RabbitMQ).
Реальная отправка через очередь — на этапе 8-9.
```

### Файлы для создания

| Файл | Назначение |
|------|-----------|
| `app/routers/auth.py` | Регистрация, логин, refresh, verify-email, logout |
| `app/routers/users.py` | Профиль пользователя |
| `app/services/auth.py` | Хеширование, JWT, верификация, валидация пароля |
| `app/repositories/user.py` | SQL-запросы к users, user_roles, refresh_tokens |
| `app/schemas/user.py` | UserCreate, UserResponse, TokenResponse, RoleResponse |
| `app/utils/security.py` | bcrypt helpers, JWT encode/decode, generate_token |
| `app/dependencies.py` | get_current_user, require_any_role |
| `app/middleware/progressive_ban.py` | Progressive rate limiting (Redis) |
| `app/middleware/request_id.py` | X-Request-ID |
| `app/middleware/sanitization.py` | Очистка HTML/XSS из текстовых полей |
| `app/middleware/error_handler.py` | Глобальная обработка ошибок |
| `app/redis.py` | Redis client, get_redis dependency |

### Ключевые концепции

- **JWT** — access token (15 мин) + refresh token (7 дней, **хранится в БД**)
- **bcrypt** — хеширование паролей (passlib или bcrypt)
- **Мульти-роли** — `user_roles` many-to-many, `require_any_role()` проверяет наличие хотя бы одной роли
- **Progressive ban** — Redis sliding window + exponential backoff (2^N секунд, макс 1 час)
- **Email verification** — токен в БД (не в JWT), SHA-256 хеш
- **Depends()** — цепочка зависимостей: `get_db → get_current_user → require_any_role`
- **Middleware** — request/response pipeline в FastAPI
- **JWT type guard** — `get_current_user` проверяет `payload["type"] == "access"`,
  чтобы будущая выдача JWT-refresh не открывала уязвимость подмены
- **Constant-time compare** — `secrets.compare_digest` для внутренних API-ключей

### SQL-фокус

| Что изучаем | Как |
|-------------|-----|
| INSERT, SELECT с WHERE | Создание пользователя, поиск по email |
| UNIQUE constraint | email должен быть уникальным |
| Many-to-many | user_roles: один пользователь — несколько ролей |
| JOIN | users + user_roles для получения профиля с ролями |
| Индексы | INDEX на email, INDEX на (user_id, role) |
| DELETE с условием | Очистка expired refresh tokens |

### Как проверить

1. `POST /auth/register` — создаёт пользователя, возвращает "проверьте email"
2. `POST /auth/register` с **тем же** email — тот же 200 "проверьте email"
   (нет user enumeration)
3. `POST /auth/verify-email?token=...` — подтверждает email
4. Повторный `POST /auth/verify-email` с тем же токеном → 400
   (атомарный mark used)
5. `POST /auth/login` — возвращает access + refresh token
6. `POST /auth/login` с несуществующим email — 401 с тем же временем
   ответа, что и с неверным паролем (dummy bcrypt verify)
7. `GET /users/me` с `Authorization: Bearer <token>` — профиль с email и ролями
8. `GET /users/me` без токена — 401
9. `GET /users/{id}` чужого юзера → 200, но **без `email`** в ответе
10. `POST /auth/refresh` — возвращает **новый** access + **новый** refresh,
    старый refresh после этого → 401 (rotation)
11. `POST /auth/logout` — refresh token отозван, повторный refresh → 401
12. Эндпоинт с `require_any_role("admin")` — 403 для обычного пользователя
13. 6 быстрых запросов на `/auth/login` — 429 с Retry-After: 2
14. Повторная попытка сразу — 429 с Retry-After: 4 (экспоненциальный рост)
15. В Swagger UI кнопка «Authorize» (через `/auth/token`) выдаёт токен
    и подставляет его в защищённые эндпоинты
