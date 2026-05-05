# Этап 11: Хинты — Онлайн-поддержка

Задачи идут в порядке реализации: сначала данные, потом логика, потом транспорт.

---

## Задача 1: `app/models/support.py` — ORM-модели

### 1. Что делать

Создать файл `app/models/support.py` с двумя моделями:

- `SupportTicket` — обращение пользователя
- `SupportMessage` — сообщение внутри обращения

Поля `SupportTicket`: `id`, `user_id` (FK → users), `subject`, `status` (Enum), `priority` (Enum), `assigned_to_id` (FK → users, nullable), `created_at`, `updated_at`.

Поля `SupportMessage`: `id`, `ticket_id` (FK → support_tickets), `sender_id` (FK → users), `message`, `is_from_operator` (bool), `is_read` (bool, default False), `created_at`.

### 2. Как это работает

SQLAlchemy Enum-столбец маппится на Python `enum.Enum`. При объявлении `Column(sa.Enum(TicketStatus))` SQLAlchemy создаёт PostgreSQL-тип `ticketstatus` (если ещё нет) и хранит строку. Relationship с `foreign_keys` нужен потому, что у `SupportTicket` два FK на одну таблицу `users` — SQLAlchemy не может сам определить, какой использовать.

### 3. API / примеры

```python
import enum
import sqlalchemy as sa
from sqlalchemy.orm import relationship
from app.models.base import Base

class TicketStatus(str, enum.Enum):
    open = "open"
    in_progress = "in_progress"
    resolved = "resolved"
    closed = "closed"

class SupportTicket(Base):
    __tablename__ = "support_tickets"

    id = sa.Column(sa.Integer, primary_key=True)
    user_id = sa.Column(sa.Integer, sa.ForeignKey("users.id"), nullable=False)
    assigned_to_id = sa.Column(sa.Integer, sa.ForeignKey("users.id"), nullable=True)
    status = sa.Column(sa.Enum(TicketStatus), default=TicketStatus.open, nullable=False)

    user = relationship("User", foreign_keys=[user_id])
    assignee = relationship("User", foreign_keys=[assigned_to_id])
    messages = relationship("SupportMessage", back_populates="ticket")
```

### 4. Зачем это нужно

Два FK на одну таблицу — типичная задача в системах поддержки (создатель ≠ исполнитель). Паттерн `foreign_keys=[...]` — стандартный способ разрешить такую двусмысленность в SQLAlchemy. Без него — ошибка `AmbiguousForeignKeysError` при старте.

### 5. Ключевые термины

- `sa.Enum(PythonEnum)` — столбец с ограничением на значения; PostgreSQL создаёт тип автоматически
- `foreign_keys=[column]` — параметр `relationship`, подсказывает SQLAlchemy какой FK использовать
- `back_populates` — двусторонняя связь: `ticket.messages` и `message.ticket` синхронизированы
- `str, enum.Enum` — миксин; значения Enum совместимы со строками (удобно в Pydantic и JSON)

### 6. Как проверить

```
docker compose exec api python -c "from app.models.support import SupportTicket, SupportMessage, TicketStatus; print(TicketStatus.open.value)"
```

Должно вывести `open` без ошибок импорта.

---

## Задача 2: Alembic-миграция для таблиц support

### 1. Что делать

Сгенерировать и выполнить миграцию, которая создаёт таблицы `support_tickets` и `support_messages`. Порядок: сначала модели готовы (задача 1), потом миграция.

```
docker compose exec api alembic revision --autogenerate -m "stage_11_support"
docker compose exec api alembic upgrade head
```

Проверить сгенерированный файл в `migrations/versions/` — убедиться, что в `upgrade()` есть `op.create_table("support_tickets", ...)` и `op.create_table("support_messages", ...)`.

### 2. Как это работает

`--autogenerate` сравнивает текущее состояние метаданных SQLAlchemy (все импортированные модели) с историей Alembic. Он замечает новые таблицы и генерирует `op.create_table`. PostgreSQL Enum-тип (`ticketstatus`, `ticketpriority`) Alembic тоже создаёт через `sa.Enum(...).create(op.get_bind())` внутри `upgrade`.

### 3. API / примеры

Фрагмент хорошей автогенерированной миграции:

```python
def upgrade() -> None:
    op.create_table(
        "support_tickets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("status", sa.Enum("open", "in_progress", "resolved", "closed",
                                    name="ticketstatus"), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
```

Если в `upgrade` этого нет — значит модели не импортированы в `migrations/env.py`.

### 4. Зачем это нужно

Без миграции таблицы не существуют в БД и любой запрос к `/support/...` упадёт с `UndefinedTableError`. Alembic — единственный способ менять схему БД не потеряв данные в продакшне.

### 5. Ключевые термины

- `--autogenerate` — флаг, включающий сравнение метаданных с БД
- `op.create_table` — функция миграции, создаёт таблицу со всеми ограничениями
- `sa.Enum(name=...)` — именованный PostgreSQL-тип; удаляется отдельно в `downgrade`
- `alembic upgrade head` — применить все не применённые миграции до последней

### 6. Как проверить

```
docker compose exec api alembic upgrade head
docker compose exec db psql -U postgres -d animaldemo -c "\dt support*"
```

Вывод должен содержать `support_tickets` и `support_messages`.

---

## Задача 3: `app/schemas/support.py` — Pydantic-схемы

### 1. Что делать

Создать файл `app/schemas/support.py` со следующими схемами:

- `TicketCreate` — `subject`, `priority` (опционально, default `normal`)
- `TicketResponse` — все поля тикета + `messages: list[MessageResponse]` (опционально)
- `TicketStatusUpdate` — `status: TicketStatus`
- `TicketAssign` — `operator_id: int`
- `MessageCreate` — `message: str`
- `MessageResponse` — все поля сообщения
- `WSMessage` — `type: str`, `text: str | None`, `token: str | None` (для WebSocket)

### 2. Как это работает

Pydantic v2 с `model_config = ConfigDict(from_attributes=True)` конвертирует ORM-объекты напрямую через `.model_validate(orm_obj)`. Enum-поля сериализуются в строки автоматически. `WSMessage` — схема для парсинга JSON-сообщений из WebSocket (и входящих, и исходящих).

### 3. API / примеры

```python
from pydantic import BaseModel, ConfigDict
from app.models.support import TicketStatus, TicketPriority

class TicketCreate(BaseModel):
    subject: str
    priority: TicketPriority = TicketPriority.normal

class MessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    sender_id: int
    message: str
    is_from_operator: bool
    created_at: datetime

class TicketResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    subject: str
    status: TicketStatus
    messages: list[MessageResponse] = []
```

### 4. Зачем это нужно

Схемы — контракт API: что принимаем, что возвращаем. Без `from_attributes=True` `.model_validate(ticket_orm)` упадёт, потому что ORM-объект не является словарём. `WSMessage` нужна чтобы не парсить JSON из WebSocket вручную.

### 5. Ключевые термины

- `ConfigDict(from_attributes=True)` — разрешает Pydantic читать атрибуты объектов (ORM) как поля
- `model_validate(obj)` — создаёт Pydantic-экземпляр из ORM-объекта или словаря
- `model_dump()` — сериализует Pydantic-объект в словарь (для JSON-ответа или WS)
- `Field(default=...)` — значение по умолчанию с валидацией

### 6. Как проверить

```python
# в python консоли внутри контейнера
from app.schemas.support import TicketCreate
t = TicketCreate(subject="Не работает авторизация")
print(t.model_dump())
# {'subject': 'Не работает авторизация', 'priority': <TicketPriority.normal: 'normal'>}
```

---

## Задача 4: `app/repositories/support.py` — SQL-запросы

### 1. Что делать

Создать файл `app/repositories/support.py` с функциями:

- `create_ticket(db, user_id, data: TicketCreate) → SupportTicket`
- `get_ticket(db, ticket_id) → SupportTicket | None`
- `get_user_tickets(db, user_id, skip, limit) → list[SupportTicket]`
- `get_all_tickets(db, status, skip, limit) → list[SupportTicket]` (для оператора)
- `update_ticket_status(db, ticket_id, status) → SupportTicket`
- `assign_ticket(db, ticket_id, operator_id) → SupportTicket`
- `create_message(db, ticket_id, sender_id, text, is_from_operator) → SupportMessage`
- `get_ticket_messages(db, ticket_id, limit, offset) → list[SupportMessage]`

### 2. Как это работает

Репозиторий — слой, инкапсулирующий SQL. Роутеры не знают про `select`, `where`, `scalars` — только вызывают функции репозитория. Для фильтрации по статусу используем `where(SupportTicket.status == status)` с опциональным аргументом. Пагинация сообщений: `ORDER BY created_at DESC LIMIT n OFFSET m` — загружаем последние, потом переворачиваем на клиенте.

### 3. API / примеры

```python
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

async def get_ticket(db: AsyncSession, ticket_id: int):
    result = await db.execute(
        select(SupportTicket)
        .where(SupportTicket.id == ticket_id)
        .options(selectinload(SupportTicket.messages))
    )
    return result.scalar_one_or_none()

async def get_ticket_messages(db: AsyncSession, ticket_id: int, limit=50, offset=0):
    result = await db.execute(
        select(SupportMessage)
        .where(SupportMessage.ticket_id == ticket_id)
        .order_by(SupportMessage.created_at.desc())
        .limit(limit).offset(offset)
    )
    return result.scalars().all()
```

### 4. Зачем это нужно

Без `selectinload` при обращении к `ticket.messages` в async-контексте SQLAlchemy выбросит `MissingGreenlet` — нельзя делать lazy load вне async-сессии. Репозиторий — единственное место где решается как грузить связи.

### 5. Ключевые термины

- `selectinload(relation)` — eager load связи отдельным SELECT (безопасно для async)
- `scalar_one_or_none()` — возвращает объект или None; `scalar_one()` — бросает если нет
- `.order_by(col.desc())` — сортировка по убыванию
- `.limit(n).offset(m)` — пагинация: взять `n` строк начиная с `m`-й
- `update(Model).where(...).values(...)` — UPDATE через Core (эффективнее чем load + set + commit)

### 6. Как проверить

После выполнения задач 1-3 (таблицы созданы):

```
docker compose exec api python -c "
import asyncio
from app.database import async_session_maker
from app.repositories import support as repo
from app.schemas.support import TicketCreate

async def test():
    async with async_session_maker() as db:
        t = await repo.create_ticket(db, 1, TicketCreate(subject='Test'))
        print(t.id, t.status)

asyncio.run(test())
"
```

---

## Задача 5: `app/services/support.py` — Бизнес-логика

### 1. Что делать

Создать файл `app/services/support.py` с функциями:

- `create_ticket(db, user_id, data) → SupportTicket` — создать тикет + поставить в очередь уведомление
- `get_ticket_or_403(db, ticket_id, current_user) → SupportTicket` — загрузить, проверить доступ (owner или operator)
- `change_status(db, ticket_id, new_status, operator) → SupportTicket` — сменить статус (только operator)
- `assign_operator(db, ticket_id, operator_id, admin) → SupportTicket` — назначить оператора
- `send_message(db, ticket_id, sender_id, text, is_operator) → SupportMessage` — сохранить сообщение в БД

### 2. Как это работает

Сервис — слой между роутером и репозиторием. Здесь живёт авторизационная проверка (не в роутере и не в репозитории). `get_ticket_or_403` — типичный паттерн: загружаем объект, проверяем права, бросаем `HTTPException(403)` если нет доступа. Это позволяет переиспользовать проверку в REST-эндпоинтах и WS-хендлере.

### 3. API / примеры

```python
from fastapi import HTTPException, status

async def get_ticket_or_403(db, ticket_id: int, current_user):
    ticket = await support_repo.get_ticket(db, ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    is_owner = ticket.user_id == current_user.id
    is_operator = current_user.role in ("operator", "admin")
    if not (is_owner or is_operator):
        raise HTTPException(status_code=403, detail="Access denied")
    return ticket

async def change_status(db, ticket_id, new_status, operator):
    if operator.role not in ("operator", "admin"):
        raise HTTPException(status_code=403, detail="Operators only")
    return await support_repo.update_ticket_status(db, ticket_id, new_status)
```

### 4. Зачем это нужно

Без сервисного слоя логика прав разбросана по роутерам — при добавлении WS-эндпоинта её придётся дублировать. Единый `get_ticket_or_403` вызывается и из REST, и из WebSocket-хендлера.

### 5. Ключевые термины

- `HTTPException(status_code, detail)` — FastAPI бросает JSON-ответ с кодом ошибки
- `current_user.role` — поле из JWT-payload; проверяется на `"operator"` / `"admin"`
- Паттерн `get_or_404 / get_or_403` — загрузка с проверкой; стандарт в FastAPI-проектах

### 6. Как проверить

```
POST /support/tickets  (с токеном обычного пользователя)
PUT /support/tickets/1/status  (с тем же токеном → 403)
PUT /support/tickets/1/status  (с токеном оператора → 200)
```

---

## Задача 6: `app/routers/support.py` — REST-эндпоинты

### 1. Что делать

Создать файл `app/routers/support.py` и зарегистрировать REST-эндпоинты (WebSocket — в задаче 8):

| Метод | Путь | Описание |
|-------|------|----------|
| POST | `/support/tickets` | Создать тикет |
| GET | `/support/tickets` | Мои тикеты |
| GET | `/support/tickets/{id}` | Тикет + история |
| PUT | `/support/tickets/{id}/status` | Сменить статус (оператор) |
| PUT | `/support/tickets/{id}/assign` | Назначить оператора (admin) |
| POST | `/support/tickets/{id}/messages` | Отправить сообщение (REST fallback) |
| GET | `/admin/support/tickets` | Все тикеты (admin/operator) |

Добавить роутер в `app/main.py`.

### 2. Как это работает

Роутер — тонкий слой: принять HTTP-запрос, достать зависимости (db, current_user), делегировать сервису, вернуть схему. Никаких SQL-запросов, никакой логики прав — всё в сервисе. `Depends(get_current_user)` автоматически декодирует JWT и возвращает объект пользователя.

### 3. API / примеры

```python
from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.security import get_current_user

router = APIRouter(prefix="/support", tags=["support"])

@router.post("/tickets", response_model=TicketResponse, status_code=status.HTTP_201_CREATED)
async def create_ticket(
    data: TicketCreate,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user),
):
    ticket = await support_service.create_ticket(db, current_user.id, data)
    return ticket

@router.get("/tickets/{ticket_id}", response_model=TicketResponse)
async def get_ticket(
    ticket_id: int,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user),
):
    return await support_service.get_ticket_or_403(db, ticket_id, current_user)
```

### 4. Зачем это нужно

REST — fallback для клиентов, которые не могут держать WS-соединение (мобильные приложения в background). `POST /support/tickets/{id}/messages` сохраняет сообщение в БД, но не отправляет через WebSocket — это нормально для REST fallback.

### 5. Ключевые термины

- `APIRouter(prefix=..., tags=[...])` — группа эндпоинтов с общим префиксом
- `Depends(get_current_user)` — DI: FastAPI вызывает функцию и передаёт результат
- `response_model=Schema` — FastAPI сериализует ответ через эту Pydantic-схему
- `status.HTTP_201_CREATED` — 201 для POST при создании ресурса

### 6. Как проверить

```bash
# Открыть http://localhost:8000/docs — должны быть видны все /support эндпоинты
# Или:
curl -X POST http://localhost:8000/support/tickets \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"subject": "Проблема с входом"}'
# Ожидаемый ответ: 201 с id нового тикета
```

---

## Задача 7: Connection Manager — управление WebSocket-соединениями

### 1. Что делать

Добавить в `app/routers/support.py` (или отдельный `app/services/ws_manager.py`) класс `ConnectionManager`:

```python
class ConnectionManager:
    def __init__(self):
        self.active: dict[int, list[WebSocket]] = {}  # ticket_id → [ws, ws, ...]

    async def connect(self, ticket_id: int, ws: WebSocket): ...
    def disconnect(self, ticket_id: int, ws: WebSocket): ...
    async def broadcast(self, ticket_id: int, message: dict): ...
```

Создать один экземпляр `manager = ConnectionManager()` на уровне модуля.

### 2. Как это работает

`manager` — синглтон в памяти процесса. При WS-подключении к тикету `N` WebSocket добавляется в `active[N]`. При отключении — удаляется. `broadcast` итерирует по всем WebSocket в `active[N]` и отправляет JSON. Важно: этот менеджер хранит соединения только текущего инстанса API — поэтому нужен Redis Pub/Sub (задача 8) для масштабирования.

### 3. API / примеры

```python
from fastapi import WebSocket

class ConnectionManager:
    def __init__(self):
        self.active: dict[int, list[WebSocket]] = {}

    async def connect(self, ticket_id: int, ws: WebSocket):
        await ws.accept()
        self.active.setdefault(ticket_id, []).append(ws)

    def disconnect(self, ticket_id: int, ws: WebSocket):
        connections = self.active.get(ticket_id, [])
        if ws in connections:
            connections.remove(ws)

    async def broadcast(self, ticket_id: int, data: dict):
        for ws in self.active.get(ticket_id, []):
            await ws.send_json(data)

manager = ConnectionManager()
```

### 4. Зачем это нужно

Без менеджера нельзя отправить сообщение второму подключённому клиенту (оператору или пользователю) — FastAPI не хранит WebSocket-объекты сам по себе. Менеджер — это реестр активных соединений.

### 5. Ключевые термины

- `WebSocket.accept()` — завершает HTTP→WS upgrade handshake; без него соединение не открыто
- `WebSocket.send_json(dict)` — сериализует в JSON и отправляет как text frame
- `WebSocket.receive_json()` — ждёт следующего text frame и десериализует из JSON
- `dict.setdefault(key, [])` — вернуть значение или создать с дефолтом, если ключа нет

### 6. Как проверить

Менеджер тестируется в рамках WS-эндпоинта (задача 8). На этом этапе достаточно убедиться, что класс импортируется без ошибок:

```
docker compose exec api python -c "from app.routers.support import manager; print(manager.active)"
# Вывод: {}
```

---

## Задача 8: WebSocket-эндпоинт с JWT и Redis Pub/Sub

### 1. Что делать

Добавить в `app/routers/support.py` WebSocket-эндпоинт:

```python
@router.websocket("/ws/{ticket_id}")
async def ws_ticket(ticket_id: int, ws: WebSocket, db: AsyncSession = Depends(get_db)):
    ...
```

Логика подключения:
1. `await manager.connect(ticket_id, ws)` — принять соединение
2. Получить первое сообщение: `{"type": "auth", "token": "eyJ..."}`
3. Декодировать JWT, проверить доступ к тикету (`get_ticket_or_403`)
4. Если ошибка — `await ws.close(code=4001)` и выйти
5. Подписаться на Redis канал `support:ticket:{ticket_id}`
6. Запустить два параллельных таска: слушать WS и слушать Redis
7. При получении сообщения от клиента: сохранить в БД, publish в Redis
8. При получении из Redis: `await manager.broadcast(ticket_id, data)`
9. При разрыве — `manager.disconnect(ticket_id, ws)`, отписаться от Redis

### 2. Как это работает

`asyncio.create_task` запускает два корутина параллельно в одном event loop. Один ждёт сообщений от клиента (`ws.receive_json()`), второй ждёт сообщений из Redis pub/sub (`pubsub.listen()`). Когда один падает (клиент отключился), мы отменяем второй через `task.cancel()`. Redis Pub/Sub работает как broadcast-шина: publish в канал → все подписчики получают.

### 3. API / примеры

```python
import asyncio
import json
from app.redis_client import redis_client  # aioredis

@router.websocket("/ws/{ticket_id}")
async def ws_ticket(ticket_id: int, ws: WebSocket, db=Depends(get_db)):
    await manager.connect(ticket_id, ws)
    try:
        # 1. Auth через первое сообщение
        auth_data = await ws.receive_json()
        user = decode_token(auth_data.get("token", ""))  # ваша функция декода JWT
        await support_service.get_ticket_or_403(db, ticket_id, user)

        # 2. Redis Pub/Sub
        pubsub = redis_client.pubsub()
        channel = f"support:ticket:{ticket_id}"
        await pubsub.subscribe(channel)

        async def listen_ws():
            async for msg in ws.iter_json():
                await support_repo.create_message(db, ticket_id, user.id, msg["text"], ...)
                await redis_client.publish(channel, json.dumps({"text": msg["text"], "from": user.id}))

        async def listen_redis():
            async for msg in pubsub.listen():
                if msg["type"] == "message":
                    await manager.broadcast(ticket_id, json.loads(msg["data"]))

        tasks = [asyncio.create_task(listen_ws()), asyncio.create_task(listen_redis())]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
    except Exception:
        pass
    finally:
        manager.disconnect(ticket_id, ws)
```

### 4. Зачем это нужно

JWT в первом сообщении (не в URL) — требование безопасности: URL попадает в access-логи веб-сервера и browser history. Redis Pub/Sub нужен при `--scale api=3`: пользователь на инстансе 1 и оператор на инстансе 2 — без Redis их менеджеры изолированы.

### 5. Ключевые термины

- `WebSocket` — класс FastAPI; `accept()`, `receive_json()`, `send_json()`, `close(code)`
- `asyncio.create_task(coro)` — запустить корутину параллельно
- `asyncio.wait(tasks, return_when=FIRST_COMPLETED)` — ждать первого завершения
- `pubsub.subscribe(channel)` — подписаться на Redis-канал
- `redis_client.publish(channel, data)` — опубликовать сообщение всем подписчикам
- WS close code `4001` — нестандартный код (1000-4999): "auth failed"

### 6. Как проверить

Используй [websocat](https://github.com/vi/websocat) или браузерную консоль:

```bash
# Terminal 1 — пользователь
websocat ws://localhost:8000/support/ws/1
# ввести: {"type":"auth","token":"<user_token>"}
# ввести: {"text":"Помогите!"}

# Terminal 2 — оператор
websocat ws://localhost:8000/support/ws/1
# ввести: {"type":"auth","token":"<operator_token>"}
# → должно прийти: {"text":"Помогите!","from":1}
```

---

## Задача 9: Уведомление оператора через RabbitMQ

### 1. Что делать

В `app/services/support.py` в функцию `create_ticket` добавить публикацию события в RabbitMQ после успешного создания тикета:

```python
await rabbit.publish(
    exchange="support",
    routing_key="ticket.created",
    body={"ticket_id": ticket.id, "subject": ticket.subject, "user_id": ticket.user_id},
)
```

Создать или дополнить воркер (в `app/workers/` или как отдельный consumer) — он слушает `ticket.created` и логирует / отправляет уведомление операторам.

### 2. Как это работает

`aio-pika` публикует сообщение в exchange. Воркер подписан на queue, привязанную к этому exchange с routing key. При создании тикета сервис публикует событие и немедленно возвращает 201 — не ждёт пока воркер обработает. Это async fire-and-forget: API не зависит от воркера.

### 3. API / примеры

```python
import aio_pika, json

async def notify_ticket_created(channel: aio_pika.Channel, ticket):
    await channel.default_exchange.publish(
        aio_pika.Message(
            body=json.dumps({
                "ticket_id": ticket.id,
                "subject": ticket.subject,
            }).encode(),
            content_type="application/json",
        ),
        routing_key="support.ticket.created",
    )

# Воркер (consumer)
async def on_ticket_created(message: aio_pika.IncomingMessage):
    async with message.process():
        data = json.loads(message.body)
        print(f"[SUPPORT] New ticket #{data['ticket_id']}: {data['subject']}")
        # здесь можно: отправить email/push оператору
```

### 4. Зачем это нужно

Уведомление оператора — side effect создания тикета. Если делать его синхронно (email-запрос в теле POST), то медленная почта замедляет API. RabbitMQ изолирует: API быстро отвечает 201, воркер делает остальное. Это паттерн event-driven: сервисы общаются через события, не через прямые вызовы.

### 5. Ключевые термины

- `aio_pika.Message(body=bytes)` — сообщение для публикации
- `exchange.publish(msg, routing_key=...)` — отправить в exchange с ключом маршрутизации
- `message.process()` — context manager: auto-ack при успехе, nack при исключении
- Fire-and-forget — паттерн: отправить и не ждать результата

### 6. Как проверить

```bash
# 1. Создать тикет
curl -X POST http://localhost:8000/support/tickets \
  -H "Authorization: Bearer <token>" \
  -d '{"subject":"Test RabbitMQ"}'

# 2. Проверить логи воркера
docker compose logs worker
# Должна быть строка: [SUPPORT] New ticket #1: Test RabbitMQ

# 3. Или через RabbitMQ Management UI
# http://localhost:15672 → Queues → support.ticket.created → Get messages
```
