# Этап 16 — Realtime-уведомления (WebSocket push)

## Цель

Доставлять in-app уведомления пользователю **в реальном времени** по WebSocket:
колокольчик/счётчик/список обновляются мгновенно, без поллинга, как только
произошло событие. Сейчас уведомления — это журнал email-рассылки (этап 9),
который фронт читает поллингом через `GET /notifications`.

## Контекст (откуда растёт)

- **Этап 9** дал пайплайн: бизнес-событие → `publish_event` → RabbitMQ topic
  exchange → events-воркер (`worker/handlers/events_handler.py`) находит
  подписчиков → создаёт `Notification(channel=email, status=pending)` +
  кладёт `EmailTaskMessage` в очередь → email-воркер шлёт SMTP и ставит
  `status=sent/failed`. Таблица `notifications` = журнал рассылки.
- **Этап 11** дал WebSocket-инфраструктуру для чата поддержки:
  `app/services/ws_manager.py` (`WSConnectionManager`) + Redis Pub/Sub для
  работы при `scale api=N`. Этот паттерн переиспользуем здесь почти 1-в-1.
- На бейджах фронта «Email/Sent» — это поля `channel`/`status` записи (канал
  доставки + статус доставки email), а не транспорт. Realtime-канала нет.

## Принятые решения (зафиксировано с пользователем 2026-06-02)

> **Важно про терминологию.** `email` в текущей системе (этап 9) — это
> ОТПРАВКА реального письма на почту (MailPit/SMTP); таблица `notifications`
> — журнал этих писем. Колокольчик в приложении — ДРУГОЕ: in-app уведомления
> БЕЗ всякой почты. Это два независимых канала одного события.

1. **Отдельный канал `in_app`** — новое значение в `NotificationChannel`.
   In-app уведомление = строка `notifications` с `channel=in_app`, которая
   показывается в колокольчике. Письмо при этом НЕ отправляется.
2. **По WS шлём САМУ запись** — готовый JSON `NotificationResponse` (та же
   форма, что в `GET /notifications`), а не «пинг: проверь сервер».
3. **Колокольчик читает ТОЛЬКО `in_app`** — `GET /notifications?channel=in_app`.
   Email-строки в ленту колокольчика не попадают **by design**, поэтому
   проблемы дублей нет и дедуп не нужен. Email-журнал — отдельная сущность.
4. **Счётчик инкрементит фронт** по входящему WS-пушу (v1). Серверный пуш
   `unread-count` — в технический долг.

## Архитектура

```
   Бизнес-событие → outbox → RabbitMQ topic exchange
                                   │
                          events-воркер (process_event)
                          ├─ создаёт Notification(channel=in_app, status=sent)
                          └─ redis.publish("notif:{user_id}", <NotificationResponse>)
                                   │
                            ┌──────┴───────┐  Redis Pub/Sub
                            ▼              ▼
                        [API #1]        [API #2]
              notif_ws_manager._listen("notif:{user_id}")  (подписка ленивая,
                            │              │                 при первом WS connect)
                            ▼              ▼
                       ws клиента A    ws клиента B  → бейдж/список обновились
```

Ключевое: **persistence и realtime разнесены**. Строка в `notifications`
сохраняется всегда (история, `GET /notifications`); WS-push — best-effort
поверх (долетает только если юзер сейчас подключён). Никто не подключён —
push просто отбрасывается Pub/Sub'ом, но при следующем `GET` юзер всё увидит.

## Решённые вопросы

1. **Аудитория in_app (v1).** events-воркер, обрабатывая событие для
   получателя, создаёт in_app-строку (+ WS-push) ОТДЕЛЬНО и независимо от
   email. То есть аудитория та же, что у текущих подписок, но строки in_app
   самостоятельные. Email на эти события можно вовсе отключить — на
   колокольчик не влияет. Отдельная пользовательская настройка in_app-подписок
   (`Subscription.channel=in_app`) — в технический долг.
2. **Дубли — решены by design.** Колокольчик запрашивает только
   `GET /notifications?channel=in_app`, поэтому email-строки в него не
   попадают и дедуп не нужен (см. Задачу 5).
3. **Счётчик — на фронте.** Фронт инкрементит бейдж по входящему WS-пушу;
   серверный пуш `unread-count` — на потом.

---

## Задача 1 — Значение `in_app` в enum + миграция

**1. Что делать.** В `app/models/notification.py` добавить в `NotificationChannel`
значение `in_app = "in_app"`. Создать Alembic-миграцию (down_revision =
текущий head `c9f3a17b8e42`), которая добавляет значение в PG-enum-тип:
```python
def upgrade():
    op.execute("ALTER TYPE notificationchannel ADD VALUE IF NOT EXISTS 'in_app'")
def downgrade():
    pass  # PG не умеет удалять значение из enum — downgrade no-op (задокументировать)
```

**2. Как это работает.** Колонка `notifications.channel` — это PG-enum
`notificationchannel` (`SAEnum(NotificationChannel, name="notificationchannel")`).
Добавить вариант в Python-enum недостаточно: БД отвергнет INSERT с неизвестным
значением. `ALTER TYPE ... ADD VALUE` расширяет сам PG-тип. В PostgreSQL 12+
команда работает внутри транзакции (Alembic так и запускает), но **новое
значение нельзя использовать в той же транзакции**, где оно добавлено — для
нас ок, мы используем его уже в рантайме приложения.

**3. API / примеры.**
```python
class NotificationChannel(str, enum.Enum):
    email = "email"
    push = "push"      # резерв
    in_app = "in_app"  # realtime через WebSocket (этап 16)
```

**4. Зачем это нужно.** Без отдельного значения in_app-уведомления были бы
неотличимы от email на уровне данных, и фронт не смог бы показывать
realtime-ленту отдельно от журнала рассылки.

**5. Ключевые термины.**
- `ALTER TYPE ... ADD VALUE` — расширение PG enum-типа новым значением.
- `op.execute` — выполнить сырой SQL в миграции.
- `SAEnum(..., name="notificationchannel")` — связка Python-enum ↔ PG-тип.

**6. Как проверить.**
```bash
docker compose exec api alembic upgrade head
docker compose exec postgres psql -U showtail -d showtail \
  -c "SELECT unnest(enum_range(NULL::notificationchannel));"
# В выводе должно быть email | push | in_app
```

---

## Задача 2 — Обобщить `WSConnectionManager` (prefix + ключ = user_id)

**1. Что делать.** В `app/services/ws_manager.py` сделать имя Redis-канала
параметром: добавить `__init__(self, channel_prefix: str)`, превратить
модульную `_channel(ticket_id)` в метод `self._channel(key)` →
`f"{self._prefix}:{key}"`. Оставить существующий синглтон поддержки
(`ws_manager = WSConnectionManager("support")`) и добавить
`notif_ws_manager = WSConnectionManager("notif")`. Ключ теперь — произвольный
UUID (для поддержки это `ticket_id`, для уведомлений — `user_id`).

**2. Как это работает.** Менеджер хранит per-instance `dict[UUID, set[WebSocket]]`
и на первый connect ключа поднимает фоновую задачу `_listen`, которая
подписывается на Redis-канал и раскидывает входящие сообщения в локальные
сокеты (см. текущую реализацию `_listen`/`_broadcast_local`). Параметризация
префикса делает класс переиспользуемым без копипасты — единственное, что
отличает «поддержку» от «уведомлений», это namespace канала.

**3. API / примеры.**
```python
class WSConnectionManager:
    def __init__(self, channel_prefix: str) -> None:
        self._prefix = channel_prefix
        self._connections: dict[uuid.UUID, set[WebSocket]] = {}
        ...
    def _channel(self, key: uuid.UUID) -> str:
        return f"{self._prefix}:{key}"

ws_manager = WSConnectionManager("support")
notif_ws_manager = WSConnectionManager("notif")
```

**4. Зачем это нужно.** Переиспользуем выверенную (bug_205/206) механику
cross-instance доставки вместо второй реализации. Меньше кода — меньше мест,
где можно ошибиться с утечкой Redis-подписок.

**5. Ключевые термины.**
- `redis_client.pubsub()` / `subscribe` / `listen` — Redis Pub/Sub API.
- `asyncio.create_task` — фоновая корутина-листенер на ключ.
- `_broadcast_local` — рассылка по сокетам текущего инстанса.

**6. Как проверить.** Юнит-тест с фейковым redis: два менеджера с разными
префиксами не пересекаются по каналам; `connect` поднимает подписку,
`disconnect` последнего сокета — отменяет её. `pytest tests/.../test_ws_manager.py`.

---

## Задача 3 — WS-эндпоинт `GET /ws/notifications`

**1. Что делать.** Новый WebSocket-роут (в `app/routers/notifications.py` или
отдельном `app/routers/notifications_ws.py`). Поток: `accept()` → первым
сообщением `{"type":"auth","token":"<access>"}` → аутентификация → 
`notif_ws_manager.connect(user.id, ws)` → цикл `receive` только для детекта
дисконнекта → в `finally` `disconnect`. Применить rate-limit на connect
(10/мин на IP — см. таблицу безопасности в README).

**2. Как это работает.** Хендшейк через первое сообщение, а не токен в URL —
URL попадает в логи прокси/историю (см. политику безопасности «WebSocket auth:
JWT передаётся первым сообщением»). В отличие от чата, клиент уведомлений
ничего не шлёт — только принимает; `receive_text()` в цикле нужен лишь чтобы
поймать `WebSocketDisconnect`. Сообщения юзеру приходят НЕ из этого цикла, а
из `notif_ws_manager._listen` (Redis → сокет).

**3. API / примеры.**
```python
@router.websocket("/ws/notifications")
async def notifications_ws(websocket: WebSocket):
    await websocket.accept()
    first = await websocket.receive_json()
    async with async_session_factory() as db:
        user = await authenticate_ws(db, first.get("token"))  # см. Задачу 3b
    if user is None:
        await websocket.close(code=4401); return
    await notif_ws_manager.connect(user.id, websocket)
    try:
        while True:
            await websocket.receive_text()  # держим соединение, ловим disconnect
    except WebSocketDisconnect:
        pass
    finally:
        await notif_ws_manager.disconnect(user.id, websocket)
```

**Задача 3b (рефактор-DRY).** Вынести `_authenticate_ws` из
`app/routers/support.py` в общий хелпер (например, `app/dependencies.py:
authenticate_ws(db, token) -> User | None`) и переиспользовать в обоих
WS-роутах. Сейчас логика декода токена и проверки `is_active` дублируется.

**4. Зачем это нужно.** Это точка, где браузер держит «живую трубу» к серверу.
Без неё push'ить некуда. Эндпоинт намеренно «тонкий»: вся cross-instance
магия — в менеджере.

**5. Ключевые термины.**
- `@router.websocket(path)` — объявление WS-роута в FastAPI.
- `websocket.accept()` / `receive_json` / `close(code=4401)` — WS API Starlette.
- `WebSocketDisconnect` — исключение при разрыве, нормальный выход из цикла.
- `notif_ws_manager.connect/disconnect` — регистрация сокета (Задача 2).

**6. Как проверить.** `wscat` или фронт:
```
wscat -c ws://localhost:8000/ws/notifications
> {"type":"auth","token":"<ACCESS_TOKEN>"}
# затем в другом терминале POST /notifications/_dev/seed (этап 16: с push)
# → в wscat прилетит {"type":"notification","payload":{...}}
```

---

## Задача 4 — Публикация in_app из events-воркера

**1. Что делать.** В `worker/handlers/events_handler.py:process_event`, в цикле
по подписчикам, помимо email-`Notification`, создавать in_app-`Notification`
(`channel=in_app`, `status=sent`, отдельный детерминированный `message_id`) и
публиковать сериализованный `NotificationResponse` в Redis-канал
`notif:{user_id}`. Убедиться, что events-воркер инициализирует Redis на старте
(`run_topic_events` в `worker/main.py` → `await init_redis()`), иначе
`redis_client is None`.

**2. Как это работает.** in_app-уведомление «доставлено» в момент создания
(SMTP не нужен) → `status=sent` сразу. `message_id` должен отличаться от
email-строки (там `uuid5(event_id, user_id)`), иначе UNIQUE-constraint
`uq_notifications_message_id` не даст вставить вторую строку — берём
`uuid5(NAMESPACE_OID, f"{event_id}:{user_id}:in_app")`. Это сохраняет
идемпотентность при redelivery (повторная обработка того же события не
плодит дубль). Публикация в Redis — fire-and-forget: долетит до подписанных
API-инстансов и дальше в сокеты.

**3. API / примеры.**
```python
in_app_msg_id = uuid.uuid5(uuid.NAMESPACE_OID, f"{event.event_id}:{user.id}:in_app")
notif = Notification(
    user_id=user.id, event_type=event.event_type,
    channel=NotificationChannel.in_app, status=NotificationStatus.sent,
    subject=subject, message_id=in_app_msg_id,
)
db.add(notif); await db.flush()
payload = NotificationResponse.model_validate(notif).model_dump(mode="json")
await db.commit()
if redis_client is not None:
    await redis_client.publish(
        f"notif:{user.id}",
        json.dumps({"type": "notification", "payload": payload}),
    )
```

**4. Зачем это нужно.** Это «передатчик»: именно здесь событие превращается в
realtime-доставку. Создание строки даёт историю/бейдж, publish — мгновенность.
Атомарность (Notification в той же транзакции, что и обработка) сохраняем по
образцу bug_231.

**5. Ключевые термины.**
- `uuid.uuid5(namespace, name)` — детерминированный UUID (идемпотентность).
- `redis_client.publish(channel, data)` — Redis Pub/Sub издатель.
- `model_dump(mode="json")` — сериализация Pydantic в JSON-safe dict (UUID/дата → строки).
- `init_redis()` — инициализация `app.redis.redis_client` в процессе воркера.

**6. Как проверить.** Подключиться `wscat` (Задача 3), затем дёрнуть любое
событие (например, через dev-seed с push или реальную публикацию `dog.title_earned`).
В сокет должен прийти JSON уведомления; в БД — строка `channel=in_app`.

---

## Задача 5 — Контракт пуша + фильтр канала в `GET /notifications`

**1. Что делать.**
- Зафиксировать формат WS-сообщения: `{"type":"notification","payload":<NotificationResponse>}`.
- Добавить в `GET /notifications` необязательный query-параметр
  `channel: NotificationChannel | None` (фильтр). Колокольчик запрашивает
  `?channel=in_app`, чтобы не смешивать realtime-ленту с журналом email.
- Репозиторий `list_user_notifications` — добавить опциональный `channel`-фильтр.

**2. Как это работает.** Один и тот же `NotificationResponse` едет и в HTTP-списке,
и в WS-пуше — фронт переиспользует один маппер. Фильтр по каналу решает проблему
дублей (email-строка + in_app-строка на одно событие при piggyback).

**3. API / примеры.**
```python
# роутер
@router.get("/notifications", response_model=list[NotificationResponse])
async def list_my_notifications(channel: NotificationChannel | None = None, ...):
    items = await repo.list_user_notifications(db, user.id, channel=channel, ...)
```

**4. Зачем нужно.** Без фильтра фронту пришлось бы фильтровать на клиенте и
бейдж считал бы дубли. Серверный фильтр — единый источник правды.

**5. Ключевые термины.**
- `Query`-параметр Enum — FastAPI сам валидирует значение канала.
- `unread-count` (этап notifications) — пересчитать с учётом канала при желании.

**6. Как проверить.**
```
GET /notifications?channel=in_app  → только in_app-строки
GET /notifications                 → все (как раньше)
```

---

## Задача 6 — Тесты

**1. Что делать.**
- Юнит: обобщённый `WSConnectionManager` (Задача 2) с фейковым Redis.
- Интеграция WS: через `fastapi.testclient.TestClient` (синхронный
  `client.websocket_connect("/ws/notifications")`) — httpx.AsyncClient WS не
  умеет. Сценарий: connect → auth → опубликовать в `notif:{user_id}` →
  получить сообщение в сокете. Требует реального Redis (как остальные
  интеграционные тесты, db 15).
- Интеграция фильтра: `GET /notifications?channel=in_app` отдаёт только in_app.

**2. Как это работает.** `TestClient.websocket_connect` — контекст-менеджер,
дающий объект с `send_json`/`receive_json`. Под капотом гоняет ASGI-WS
синхронно, что удобно для детерминированных проверок.

**3. API / примеры.**
```python
from fastapi.testclient import TestClient

with TestClient(app).websocket_connect("/ws/notifications") as ws:
    ws.send_json({"type": "auth", "token": access_token})
    # publish в notif:{uid} из теста → 
    data = ws.receive_json()
    assert data["type"] == "notification"
```

**4. Зачем нужно.** WS-путь и cross-process publish легко регрессят молча —
тест ловит «push не доехал» до того, как это увидит фронт.

**5. Ключевые термины.**
- `TestClient.websocket_connect` — синхронный WS-клиент Starlette для тестов.
- фикстуры `test_redis` / `db_session` из `tests/integration/conftest.py`.

**6. Как проверить.** `pytest tests/integration/test_notifications_ws.py -v` — зелёный.

---

## Критерии готовности этапа (для stage-verification)

- [ ] `notificationchannel` в БД содержит `in_app` (миграция накатана).
- [ ] `WSConnectionManager` параметризован префиксом; поддержка и уведомления
      используют разные синглтоны, тесты менеджера зелёные.
- [ ] `GET /ws/notifications` принимает соединение, аутентифицирует первым
      сообщением, регистрирует/снимает сокет, держит соединение.
- [ ] events-воркер создаёт `channel=in_app` строку и публикует её в
      `notif:{user_id}`; Redis в воркере инициализирован.
- [ ] Подключённый клиент получает `{"type":"notification","payload":{...}}`
      в реальном времени при событии.
- [ ] `GET /notifications?channel=in_app` фильтрует ленту.
- [ ] `_authenticate_ws` вынесен в общий хелпер и переиспользован.
- [ ] Интеграционные + юнит тесты на WS и фильтр зелёные; полный прогон
      `pytest -q` без регрессий.

## Связанные точки кода

- Паттерн WS+Redis: `app/services/ws_manager.py`, `app/routers/support.py`
  (auth первым сообщением, `connect/disconnect`, per-message session).
- Точка создания уведомлений: `worker/handlers/events_handler.py:process_event`.
- Модель/каналы: `app/models/notification.py` (`NotificationChannel`,
  `Notification`).
- Ответ API: `app/schemas/notification.py` (`NotificationResponse` с `is_read`).
- Инициализация Redis: `app/redis.py` (`init_redis`, `redis_client`).

## Технический долг / на потом

- Серверный пуш `unread-count` (а не инкремент на клиенте).
- in_app-подписки как отдельная пользовательская настройка (Вариант B).
- Graceful shutdown pubsub-листенеров уведомлений (то же, что отмечено для
  поддержки в этапе 14).
- Rate-limit и backpressure на «болтливых» событиях (батч-пуш).
