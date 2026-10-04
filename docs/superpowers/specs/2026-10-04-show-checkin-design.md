# Дизайн: регистрация прибытия участников на выставку (check-in)

**Дата:** 2026-10-04
**Статус:** на ревью

## Проблема

Участник записывает собаку на выставку заранее (`ShowEntry`), но в системе
нет способа отметить, что собака реально приехала, прошла ветконтроль и
проверку документов. Организатор не знает, кто допущен, кто не явился, а
результаты можно внести для любой записи. Документы собаки (ветпаспорт,
родословная) в системе не хранятся — их проверяют только на бумаге, в
очереди у стойки регистрации.

## Цель и критерий успеха

Регистратор сканирует QR участника (или находит его поиском) и за 10–20
секунд видит его собак, флаги «документы предварительно одобрены» и
«прививка действительна на дату выставки», ставит отметки и выдаёт номер.
Организатор в любой момент видит сводку: прибыло / допущено / не допущено /
ожидается; не явившиеся отмечаются автоматически и не попадают в результаты.

## Решения (утверждены пользователем)

1. **На стойке в v1:** прибытие + ветконтроль + проверка документов. Оплата
   на месте — вне объёма.
2. **Документы загружаются заранее** и проверяются на месте экспресс-сверкой.
3. **Документы принадлежат собаке** и переиспользуются между выставками;
   **допуск решает организатор каждой выставки** — отметка ставится на запись,
   а не на файл.
4. **Один QR на человека на выставку**; отметки — по каждой записи (собаке).
   Ручной поиск — всегда доступен как запасной путь.
5. **Только онлайн**; QR подписан HMAC, без хранения в БД.
6. **Хранение состояния — подход «статус в записи + журнал проверок»:**
   короткий `attendance_status` в `ShowEntry` для каталога/результатов,
   подробности — в append-only таблице `entry_checks`.
7. **Статус пересчитывается автоматически** из проверок, отдельной кнопки
   «допустить» на уровне модели нет (во фронте «Допустить» = пачка отметок).
8. **Регистратор добавляется по email или телефону**, без ссылок-приглашений.
9. **Чек-ин включается флагом выставки** `checkin_enabled` — выставки без
   чек-ина работают как раньше.
10. **В v1 входят:** напоминание о недостающих/просроченных документах,
    автоматическая неявка, роль регистратора на выставке.

## Вне объёма v1

- Подтверждение участия по SMS/push за 1–2 дня до выставки.
- Офлайн-режим стойки (кэш в IndexedDB, очередь отметок).
- Приём оплаты на месте.
- PDF-билет и QR на запись (только QR на человека).
- Ссылки-приглашения для регистраторов.
- Live-обновления стойки через WebSocket (используется polling).
- Отдельная модерация документов модератором платформы.

## Допущения

- Регистраторы работают с телефона в браузере (web), отдельного приложения
  нет. Камера доступна только по HTTPS/`localhost` — в проде HTTPS есть.
- Если cron напоминаний не отработал в нужный день — напоминания не будет
  (приемлемо для v1).
- Отозванные записи по-прежнему удаляются существующим
  `DELETE /shows/{id}/entries/{entry_id}`; статуса `withdrawn` не вводим.

## Модель данных

### `dog_documents` (новая)

| поле | тип | смысл |
|---|---|---|
| `id` | UUID PK | |
| `dog_id` | FK → `dogs` CASCADE, index | чья собака |
| `file_id` | FK → `files` CASCADE, index | скан; файл создаётся с `is_public=False` (ПДн + ветданные) |
| `kind` | enum `dogdocumentkind` | `vet_passport`, `pedigree`, `puppy_card`, `working_certificate`, `other` |
| `valid_until` | date, nullable | для `vet_passport` — срок действия прививки от бешенства; вводит владелец |
| `uploaded_by` | FK → `users` SET NULL, nullable | |
| `created_at` | timestamptz | |

Одного вида может быть несколько документов (новый ветпаспорт после
ревакцинации). **Действующий** документ вида = последний по `created_at`.

`working_certificate` закрывает заглушку `REQUIRES_DOCS_NOTES` в
`app/services/show_rules.py`: для рабочего класса можно показывать, загружен
ли сертификат, а не только предупреждать.

### `show_staff` (новая)

| поле | тип | смысл |
|---|---|---|
| `id` | UUID PK | |
| `show_id` | FK → `shows` CASCADE, index | |
| `user_id` | FK → `users` CASCADE, index | |
| `role` | enum `showstaffrole` | пока только `registrar` |
| `added_by` | FK → `users` SET NULL, nullable | |
| `created_at` | timestamptz | |

`UNIQUE (show_id, user_id, role)`. Глобальная `RoleEnum` не меняется —
регистратор — роль в рамках одной выставки.

### `shows.checkin_enabled` (новая колонка)

`Boolean NOT NULL DEFAULT false`. Редактируется организатором в настройках
выставки в любом статусе, кроме `completed`/`cancelled`. Вся логика
неявки, блокировки результатов и напоминаний работает только при `true`.

### `show_entries.attendance_status` (новые колонки)

- `attendance_status` — enum `attendancestatus`: `registered` (default),
  `arrived`, `admitted`, `rejected`, `absent`; `NOT NULL`,
  `server_default='registered'`, index.
- `attendance_changed_at` — timestamptz, nullable.

Миграция: существующие записи получают `registered`; т.к. у существующих
выставок `checkin_enabled=false`, на их поведение это не влияет.

### `entry_checks` (новая, append-only)

| поле | тип | смысл |
|---|---|---|
| `id` | UUID PK | |
| `entry_id` | FK → `show_entries` CASCADE, index | |
| `kind` | enum `entrycheckkind` | `docs_precheck`, `arrival`, `vet`, `docs_onsite` |
| `result` | enum `entrycheckresult` | `passed`, `failed` |
| `document_id` | FK → `dog_documents` SET NULL, nullable | на какой документ смотрели |
| `comment` | text, nullable | обязателен при `failed` (валидация в схеме) |
| `performed_by` | FK → `users` SET NULL, nullable | |
| `created_at` | timestamptz | |

Строки не обновляются и не удаляются (кроме каскада). Действующая отметка
вида = последняя строка этого вида. Ошибка регистратора исправляется новой
отметкой — история сохраняется, это же и аудит.

### Пересчёт `attendance_status`

Чистая функция `compute_attendance_status(latest_checks, current_status)`
в `app/services/checkin_rules.py`, вызывается после каждой пачки отметок:

1. Если последняя `vet` или `docs_onsite` — `failed` → `rejected`.
2. Иначе если последние `arrival`, `vet`, `docs_onsite` — все `passed` →
   `admitted`.
3. Иначе если последняя `arrival` — `passed` → `arrived`.
4. Иначе — текущий статус без изменений (`registered` или `absent`).

`docs_precheck` на статус не влияет — это подсказка для стойки.
Отметка опоздавшего переводит `absent` → `arrived`/`admitted` по тем же
правилам. `attendance_changed_at` обновляется, только если статус изменился.

## QR-токен

**Принцип:** токен **идентифицирует, но не авторизует**. Он лишь ускоряет
поиск участника; права даёт роль регистратора, допуск — проверка собаки на
месте (сверка чипа/клейма, ветпаспорта). Утёкший скриншот QR постороннему
ничего не даёт.

**Формат:**

```
SR1.<base64url(show_id 16 байт || user_id 16 байт)>.<base64url(HMAC-SHA256[:16])>
```

- ~70 символов → QR версии 4–5, читается бюджетной камерой.
- Без ПДн — только UUID. Без padding в base64url.
- HMAC считается над строкой `SR1.<payload>`; ключ — новая настройка
  `checkin_token_secret` в `app/config.py` (отдельно от JWT-секрета;
  обязательна в prod, как остальные секреты).
- `SR1` — версия: ротация ключа или смена формата → `SR2`, старые токены
  дают понятную ошибку `unsupported_version`.
- Сравнение подписи — `hmac.compare_digest`.
- Срока действия нет: привязка к `show_id`, после `completed` стойка
  работает только на чтение.

Модуль `app/utils/checkin_token.py`:

- `make_token(show_id: UUID, user_id: UUID) -> str`
- `parse_token(token: str) -> tuple[UUID, UUID]` — бросает
  `InvalidCheckinToken(reason)` с `reason` ∈ `malformed`,
  `unsupported_version`, `bad_signature`.

## API

Новые роутеры: `app/routers/checkin.py` (префикс `/shows`, `shows.py` уже
~600 строк) и `app/routers/dog_documents.py` (префикс `/dogs`). Логика —
`app/services/checkin.py`, `app/services/dog_document.py`; доменные правила —
`app/services/checkin_rules.py`.

### Документы собаки

Права: владелец собаки, admin (как у остальных мутаций собаки).

| метод | путь | описание |
|---|---|---|
| POST | `/dogs/{dog_id}/documents` | multipart: `file`, `kind`, `valid_until?`; квоты и проверка типа — через существующий `upload_quota`; допустимы PDF и изображения |
| GET | `/dogs/{dog_id}/documents` | список, флаг `is_current` у действующего документа каждого вида |
| DELETE | `/dogs/{dog_id}/documents/{doc_id}` | удаление; ссылки в `entry_checks.document_id` → NULL |
| GET | `/dogs/{dog_id}/documents/{doc_id}/download` | ACL ниже |

ACL скачивания: владелец собаки; admin; организатор или `show_staff`
выставки, где у этой собаки есть запись и статус выставки не
`completed`/`cancelled`. Иначе — 404.

### Персонал выставки

Права: организатор этой выставки, admin.

| метод | путь | описание |
|---|---|---|
| GET | `/shows/{id}/staff` | список регистраторов |
| POST | `/shows/{id}/staff` | `{email}` или `{phone}`; пользователь не найден → 404 `user_not_found`; уже добавлен → 409 |
| DELETE | `/shows/{id}/staff/{user_id}` | |
| GET | `/shows/staff/my` | выставки, где текущий пользователь — персонал (для меню кабинета) |

### Участник

| метод | путь | описание |
|---|---|---|
| GET | `/shows/{id}/my-ticket` | `{token, entries: [...]}` — записи, где пользователь `registered_by`, `handler_id` или владелец собаки; с предупреждениями по документам. Нет записей → 404. Доступно при `checkin_enabled` и статусе `registration_open`/`registration_closed`/`in_progress` (до закрытия регистрации номер каталога — `null`) |

### Стойка

Права: организатор этой выставки, `show_staff`, admin. Все эндпоинты требуют
`checkin_enabled`, иначе 409 `checkin_disabled`.

| метод | путь | описание |
|---|---|---|
| POST | `/shows/{id}/checkin/scan` | `{token}` → карточка участника |
| GET | `/shows/{id}/checkin/search?q=` | по номеру каталога, телефону, чипу/клейму, кличке → список карточек; rate limit через `check_rate_limit` (перебор ПДн) |
| POST | `/shows/{id}/entries/{entry_id}/checks` | `{checks: [{kind, result, comment?, document_id?}]}` — атомарно; ответ — обновлённое состояние записи |
| GET | `/shows/{id}/entries/{entry_id}/checks` | история отметок |
| GET | `/shows/{id}/checkin/summary` | счётчики по `attendance_status` |
| GET | `/shows/{id}/checkin/precheck-queue` | записи, у собак которых есть документы, но нет `docs_precheck` |

**Карточка участника:** пользователь (ФИО, телефон) и список записей; по
каждой записи: собака (кличка, фото, чип, клеймо, номер РКФ), класс, номер
каталога, `attendance_status`, действующие документы по видам, флаг
`rabies_valid_for_show` (`valid_until ≥ date_end ?? date_start`), флаг
`working_certificate_required/present`, последние отметки по каждому виду.

**Окна по статусу выставки:**

| действие | разрешённые статусы |
|---|---|
| `docs_precheck`, precheck-queue | `registration_open`, `registration_closed` |
| `arrival`, `vet`, `docs_onsite`, scan, search | `registration_closed`, `in_progress` |
| остальное в `completed`/`cancelled` | только чтение |

**Ошибки:**

| код | когда |
|---|---|
| 403 | пользователь не организатор/персонал этой выставки |
| 404 | запись не принадлежит выставке (не 403 — не раскрываем существование) |
| 409 `invalid_show_status` | отметка вне окна статусов |
| 409 `checkin_disabled` | у выставки выключен чек-ин |
| 422 | `failed` без комментария; `document_id` не принадлежит собаке записи |
| 400 `invalid_token` | `parse_token` упал (с `reason`) |
| 404 `token_other_show` | токен валиден, но `show_id` другой |
| 404 `no_entries` | у пользователя токена нет записей на выставке |

**Конкурентность:** `POST .../checks` берёт `SELECT FOR UPDATE` на строку
записи, добавляет строки проверок и пересчитывает статус в одной транзакции.
Две стойки на одну собаку — побеждает последняя отметка, обе в истории.

## Фоновые задачи и интеграции

### Автоматическая неявка

В существующем `show.change_status` (`app/services/show.py:127`): при
переходе в `in_progress` и `checkin_enabled` —
`UPDATE show_entries SET attendance_status='absent', attendance_changed_at=now()
WHERE show_id=:id AND attendance_status='registered'`. Та же транзакция и
тот же `SELECT FOR UPDATE` на выставку, что и смена статуса.

### Напоминание о документах

Новая cron-задача `remind_missing_documents` в `app/services/scheduler.py`:
ежедневно в 10:00, под существующим Redis-локом.

1. Выставки с `checkin_enabled`, статусом `registration_open` или
   `registration_closed` и `date_start = today + 3`.
2. По каждой записи — проблемы: нет действующего `vet_passport`;
   `valid_until < date_end ?? date_start`; нет `pedigree` и нет
   `puppy_card`; класс из `REQUIRES_DOCS_NOTES` (рабочий) без
   `working_certificate`.
3. Группировка по получателю (`registered_by`) → одно событие
   `show.documents_missing` на человека через `notif_svc.publish_event`
   (outbox). Payload: `show_id`, `show_name`, `date_start`, список
   `{dog_name, problems[]}`. Существующий обработчик событий доставляет
   в уведомления / WS / email; в тексте — ссылка на «Мой билет».
4. Дедупликация: Redis `SET NX` ключа `reminder:docs:{show_id}:{user_id}`
   с TTL 7 дней.

Определение проблем — чистая функция в `checkin_rules.py` (переиспользуется
предупреждениями в `my-ticket`).

### Результаты

В `app/services/result.py` (`upsert_class_result` и установка лучших): при
`checkin_enabled` и `attendance_status ∈ {absent, rejected}` → 409
`entry_not_admitted`. `registered`/`arrived`/`admitted` допустимы
(опоздавших отмечают по ходу выставки). В ответы API по записям добавляется
`attendance_status`.

## Фронтенд

Стек: Next.js + MUI (Minimal kit) + SWR. Новые зависимости: `qrcode.react`
(рендер QR), `qr-scanner` (сканирование; web worker, работает в iOS Safari).

**Участник**

- `dashboard/dogs/[id]` → вкладка «Документы»: загрузка с выбором вида; для
  ветпаспорта поле «прививка от бешенства действительна до»; список,
  действующий подсвечен.
- `dashboard/my-shows/[showId]/ticket` — «Мой билет»: крупный QR (на
  мобильном — полноэкранно), список собак с номерами каталога,
  предупреждения по документам со ссылкой на загрузку. Ссылка на билет в
  карточке выставки при `checkin_enabled` и статусе
  `registration_open`/`registration_closed`/`in_progress` (напоминание за
  3 дня может прийти до закрытия регистрации).

**Организатор**

- Настройки выставки: переключатель «Регистрация прибытия (чек-ин)».
- `dashboard/shows/[id]/staff` — регистраторы: список, добавление по email
  или телефону, удаление.
- `dashboard/shows/[id]/precheck` — очередь предпроверки: запись + просмотр
  скана; «Одобрить» / «Отклонить с комментарием».

**Стойка** — `dashboard/shows/[id]/checkin` (mobile-first; организатор и
регистраторы):

- Шапка: счётчики прибыло / допущено / не допущено / ожидается; SWR
  polling каждые 15 с.
- Кнопки «Сканировать» (полноэкранная камера, закрывается по распознаванию)
  и поиск.
- Карточка участника — собаки; по каждой: фото, кличка, номер каталога,
  чип/клеймо; бейджи «Документы предодобрены», «Прививка до ДД.ММ.ГГГГ»
  (зелёный) / «Просрочена» (красный);
  - **«Допустить»** — одна пачка `arrival`+`vet`+`docs_onsite` = `passed`;
  - **«Не допустить»** — причина (ветконтроль/документы) + обязательный
    комментарий;
  - **«Только прибыла»** — если ветконтроль за отдельным столом;
  - «История» — журнал отметок.
- Тексты ошибок скана: «QR не распознан, найдите участника вручную»,
  «Билет на другую выставку», «Нет записей на эту выставку».
- Пункт меню «Регистрация на выставке» — если `GET /shows/staff/my` не пуст.

## Тестирование

**Unit (backend)**

- `checkin_token`: round-trip; подмена любого байта payload/подписи;
  чужая версия; мусор, пустая строка, лишние точки.
- `compute_attendance_status`: все комбинации отметок, включая исправление
  `failed` → `passed` и опоздавшего из `absent`.
- Проблемы с документами: нет документа, просрочка на дату окончания,
  метрика вместо родословной, рабочий класс без сертификата.

**Integration (backend, `tests/integration`)**

- Матрица прав: участник, чужой организатор, регистратор этой выставки,
  регистратор другой выставки, admin — для стойки, staff, документов.
- Скан: валидный, чужая выставка, без записей, битый токен.
- Поиск по каждому полю; rate limit.
- Пачка отметок атомарна (одна невалидная → ничего не записано).
- Неявка при `in_progress` с `checkin_enabled` и без.
- Блокировка результатов для `absent`/`rejected`; `registered` проходит.
- ACL скачивания документа (включая выставку в `completed`).
- 404 на запись чужой выставки.
- Задача напоминаний: выборка выставок, группировка по получателю,
  дедупликация.

**Frontend**

- vitest: хелперы бейджей (валидность прививки, предупреждения).
- Playwright: happy-path стойки через поиск (камера в CI не эмулируется).

## Порядок реализации (ориентир для плана)

1. Модели + миграция (`dog_documents`, `show_staff`, `entry_checks`,
   колонки `shows.checkin_enabled`, `show_entries.attendance_status`).
2. `checkin_token` + `checkin_rules` (чистые функции, unit-тесты).
3. Документы собаки: API + ACL.
4. Персонал выставки: API.
5. Стойка: scan/search/checks/summary/precheck-queue, my-ticket.
6. Интеграции: неявка в `change_status`, блокировка в `result.py`,
   напоминания в scheduler.
7. Фронтенд: документы → билет → staff/precheck → стойка.
