# Дизайн: JWT в httpOnly-куках (веб) + body-режим (мобилка)

**Дата:** 2026-06-12
**Статус:** утверждён

## Проблема

Сейчас оба токена возвращаются в теле ответа, фронт хранит их в localStorage —
уязвимо к XSS. Существующий механизм `AUTH_REFRESH_COOKIE` — глобальный флаг:
либо все клиенты получают куку, либо все — тело. С появлением React
Native-приложения нужны оба режима одновременно: веб — куки, мобилка — тело.

## Решения (утверждены пользователем)

1. **Оба токена в httpOnly-куках** для веба — единый способ хранения,
   JS не видит токены вообще.
2. **Режим выбирается per-request** при логине, без глобального флага.
3. **Прод-топология — один домен**: nginx отдаёт SPA на `/`, проксирует
   API под `/api/` → куки first-party, `SameSite=Strict`.
4. **CSRF — вариант A**: SameSite=Strict + middleware-проверка `Origin`
   на мутациях. Без double-submit-токена (YAGNI, можно добавить позже).

## Архитектура

### Выбор режима доставки

Заголовок `X-Token-Delivery: body` в запросе логина → токены в теле ответа
(React Native). Без заголовка (дефолт, веб) → оба токена в httpOnly-куках,
в теле `access_token` и `refresh_token` равны `null`
(`TokenResponse.access_token` становится `str | None`).

Затронутые эндпоинты: `/auth/login`, `/auth/token`, `/auth/refresh`,
`/auth/verify-code`. Глобальный флаг `AUTH_REFRESH_COOKIE` удаляется.
Body-режим остаётся полноправным путём — функционал токенов в теле
не удаляется.

### Куки

| Кука | path | max_age | атрибуты |
|---|---|---|---|
| `access_token` | `/` | `access_token_expire_minutes` | httpOnly, Secure (вне debug), SameSite=Strict |
| `refresh_token` | `/auth` | `refresh_token_expire_days` | те же |

`path=/auth` у refresh-куки — минимизация поверхности: она нужна только
refresh/logout. Новая настройка `cookie_path_prefix: str = ""` — когда API
в проде переедет под `/api/`, path кук должен совпадать с публичным путём
(`/api` + `/auth`), без переделок кода.

### Чтение access-токена (`app/dependencies.py`)

`get_current_user`, `get_current_user_optional`, `authenticate_ws`:
заголовок `Authorization` (приоритет — мобилка, Swagger) → кука
`access_token`. Дальнейшая валидация без изменений. WebSocket: браузер сам
прикладывает куки при хендшейке (`websocket.cookies`); первый кадр с
токеном остаётся для мобилки.

### Извлечение refresh (`/auth/refresh`, `/auth/logout`)

`_extract_refresh`: тело → кука, кука читается всегда (раньше — только при
включённом флаге). Logout всегда чистит обе куки.

### CSRF-middleware

На POST/PUT/PATCH/DELETE: если заголовок `Origin` присутствует и не входит
в список разрешённых (свой хост + `cors_allow_origins`) → 403. Запросы без
`Origin` (мобилка, curl) проходят — у них нет автоматических кук, CSRF им
не грозит. Второй рубеж после SameSite=Strict.

### Тесты

- cookie-режим: логин ставит обе куки, тело с `null`;
- `X-Token-Delivery: body`: токены в теле, кук нет;
- refresh через куку, rotation (старая кука → 401);
- dual-извлечение access: заголовок и кука;
- CSRF: чужой `Origin` на мутации → 403, свой → проходит, без Origin → проходит;
- logout чистит обе куки.

## Вне скоупа (YAGNI)

- double-submit CSRF-токен;
- отдельные mobile-эндпоинты;
- миграция веб-фронта (пользователь делает отдельно).
