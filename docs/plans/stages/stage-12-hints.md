# Этап 12: Хинты — Админка и аналитика

Задачи идут в порядке реализации: сначала охрана доступа, потом схемы, потом логика, потом транспорт.

---

## Задача 1: `app/dependencies/admin.py` — зависимость `require_admin`

### 1. Что делать

Создать файл `app/dependencies/admin.py` с одной зависимостью:

```python
async def require_admin(current_user = Depends(get_current_user)) -> User:
    ...
```

Функция проверяет `current_user.role == "admin"`. Если нет — бросает `HTTPException(403)`. Если да — возвращает `current_user`.

### 2. Как это работает

FastAPI разрешает зависимости рекурсивно: `require_admin` сам вызывает `get_current_user`, тот читает JWT. Если проверка роли падает — весь цепочка прерывается и клиент получает 403 ещё до входа в тело эндпоинта. Сам роутер только пишет `Depends(require_admin)` и не знает про JWT.

### 3. API / примеры

```python
from fastapi import Depends, HTTPException, status
from app.security import get_current_user
from app.models.user import User

async def require_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return current_user
```

Использование в роутере:

```python
@router.get("/admin/dashboard")
async def dashboard(admin: User = Depends(require_admin)):
    ...
```

### 4. Зачем это нужно

Без единой зависимости проверка `if user.role != "admin": raise 403` будет дублироваться в каждом из 11 эндпоинтов этапа. Одна зависимость — один источник правды. Потом легко расширить: добавить `require_organizer` по аналогии.

### 5. Ключевые термины

- `Depends(другая_зависимость)` — зависимость может вызывать другую зависимость; дерево разрешается автоматически
- `HTTPException(403)` — стандартный способ прервать обработку запроса из зависимости
- Role-based access control (RBAC) — модель прав доступа по роли пользователя

### 6. Как проверить

```bash
# Запрос без токена → 401
curl http://localhost:8000/admin/dashboard

# Запрос с токеном обычного пользователя → 403
curl http://localhost:8000/admin/dashboard \
  -H "Authorization: Bearer <user_token>"

# Запрос с токеном admin → 200
curl http://localhost:8000/admin/dashboard \
  -H "Authorization: Bearer <admin_token>"
```

---

## Задача 2: `app/schemas/admin.py` — Pydantic-схемы

### 1. Что делать

Создать файл `app/schemas/admin.py` со схемами:

**Входящие (запросы):**
- `ModerationAction` — `status: str` ("approved" / "rejected"), `rejection_reason: str | None`
- `KennelVerifyAction` — `notes: str | None`
- `UserBlockAction` — `is_blocked: bool`, `reason: str | None`
- `UserRoleUpdate` — `role: str`

**Исходящие (ответы):**
- `PlatformStats` — `total_users`, `verified_kennels`, `total_dogs`, `completed_shows`, `active_classifieds` (все `int`)
- `ShowAnalyticsRow` — `breed: str`, `entries_count: int`
- `AdAnalyticsRow` — `day: datetime`, `impressions: int`, `clicks: int`, `ctr_percent: float`
- `ShowReportRow` — `breed: str`, `show_class: str`, `entries: int`, `excellent_count: int`, `revenue: float`

### 2. Как это работает

Схемы Raw SQL возвращают строки из `db.execute(text(...))` — это `RowMapping`-объекты (похожи на словари). `model_validate(dict(row))` работает при `from_attributes=True` или при передаче словаря напрямую. Схемы для аналитики — только `BaseModel`, без ORM-конфига.

### 3. API / примеры

```python
from pydantic import BaseModel, ConfigDict
from datetime import datetime

class ModerationAction(BaseModel):
    status: str  # "approved" | "rejected"
    rejection_reason: str | None = None

class PlatformStats(BaseModel):
    total_users: int
    verified_kennels: int
    total_dogs: int
    completed_shows: int
    active_classifieds: int

class AdAnalyticsRow(BaseModel):
    day: datetime
    impressions: int
    clicks: int
    ctr_percent: float

class ShowReportRow(BaseModel):
    breed: str
    show_class: str
    entries: int
    excellent_count: int
    revenue: float
```

### 4. Зачем это нужно

Аналитические схемы документируют контракт SQL-запроса. Если запрос вернул колонку с опечаткой — Pydantic поймает сразу при валидации, а не когда фронтенд упадёт с undefined. `ModerationAction` с `status` вместо двух отдельных эндпоинтов (approve / reject) — один PUT делает оба действия.

### 5. Ключевые термины

- `RowMapping` — dict-подобный результат Raw SQL из SQLAlchemy; конвертируется через `dict(row)`
- `model_validate(dict)` — создать Pydantic-экземпляр из словаря
- Union-тип в строке: `str` с ограниченными значениями — альтернатива Enum когда не нужен PostgreSQL-тип

### 6. Как проверить

```
docker compose exec api python -c "from app.schemas.admin import PlatformStats, ModerationAction; print(ModerationAction(status='approved').model_dump())"
# {'status': 'approved', 'rejection_reason': None}
```

---

## Задача 3: `app/services/moderation.py` — бизнес-логика модерации

### 1. Что делать

Создать файл `app/services/moderation.py` с функциями:

- `approve_classified(db, classified_id) → Classified` — статус → `"active"`
- `reject_classified(db, classified_id, reason) → Classified` — статус → `"rejected"`, сохранить причину
- `verify_kennel(db, kennel_id, notes) → Kennel` — `is_verified = True`
- `block_user(db, user_id, is_blocked, reason) → User` — `is_active = not is_blocked`
- `change_user_role(db, user_id, new_role) → User` — обновить роль

### 2. Как это работает

Сервис делает UPDATE через Core SQLAlchemy: `update(Model).where(...).values(...).returning(Model)` — один запрос без предварительного SELECT. Паттерн `returning` возвращает обновлённую строку сразу, что эффективнее чем load + set + commit + refresh.

### 3. API / примеры

```python
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.classified import Classified

async def approve_classified(db: AsyncSession, classified_id: int) -> Classified:
    result = await db.execute(
        update(Classified)
        .where(Classified.id == classified_id)
        .values(status="active")
        .returning(Classified)
    )
    await db.commit()
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Classified not found")
    return row

async def block_user(db: AsyncSession, user_id: int, is_blocked: bool) -> User:
    result = await db.execute(
        update(User)
        .where(User.id == user_id)
        .values(is_active=not is_blocked)
        .returning(User)
    )
    await db.commit()
    return result.scalar_one()
```

### 4. Зачем это нужно

`.returning()` — PostgreSQL-специфичная оптимизация: не нужен отдельный SELECT после UPDATE. Без неё придётся делать `await db.get(Model, id)` после commit — лишний запрос к БД. Сервис изолирует логику: роутер не знает как именно модерировать.

### 5. Ключевые термины

- `update(Model).values(...)` — SQLAlchemy Core UPDATE; не загружает объект в сессию
- `.returning(Model)` — PostgreSQL: вернуть обновлённую строку как ORM-объект
- `scalar_one()` — взять единственный результат; бросит если нет или больше одного
- `await db.commit()` — применить транзакцию; без этого изменения не сохранятся

### 6. Как проверить

```bash
# После выполнения задач 4–5 (роутеры готовы):
curl -X PUT http://localhost:8000/admin/moderation/classifieds/1 \
  -H "Authorization: Bearer <admin_token>" \
  -H "Content-Type: application/json" \
  -d '{"status": "approved"}'
# → 200 с объектом объявления, status = "active"
```

---

## Задача 4: `app/repositories/analytics.py` — Raw SQL запросы

### 1. Что делать

Создать файл `app/repositories/analytics.py` с функциями:

- `get_platform_stats(db) → PlatformStats`
- `get_show_analytics(db, period_start: datetime) → list[ShowAnalyticsRow]`
- `get_ad_analytics(db, period_start: datetime) → list[AdAnalyticsRow]`
- `get_show_report(db, show_id: int) → list[ShowReportRow]`

Использовать `text(sql)` из SQLAlchemy и именованные параметры (`:param_name`).

### 2. Как это работает

`db.execute(text(sql), {"param": value})` выполняет Raw SQL и возвращает `CursorResult`. `.mappings().all()` конвертирует строки в список `RowMapping` — dict-подобных объектов. Затем каждая строка конвертируется в Pydantic-схему через `model_validate(dict(row))`. Параметры передаются через `{"name": value}` — защита от SQL-инъекций.

### 3. API / примеры

```python
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from app.schemas.admin import PlatformStats, ShowAnalyticsRow
from datetime import datetime

async def get_platform_stats(db: AsyncSession) -> PlatformStats:
    sql = text("""
        SELECT
            (SELECT COUNT(*) FROM users WHERE is_active) AS total_users,
            (SELECT COUNT(*) FROM kennels WHERE is_verified) AS verified_kennels,
            (SELECT COUNT(*) FROM dogs) AS total_dogs,
            (SELECT COUNT(*) FROM shows WHERE status = 'completed') AS completed_shows,
            (SELECT COUNT(*) FROM classifieds WHERE status = 'active') AS active_classifieds
    """)
    result = await db.execute(sql)
    row = result.mappings().one()
    return PlatformStats.model_validate(dict(row))

async def get_show_analytics(db: AsyncSession, period_start: datetime) -> list[ShowAnalyticsRow]:
    sql = text("""
        SELECT b.name AS breed, COUNT(se.id) AS entries_count
        FROM show_entries se
        JOIN dogs d ON se.dog_id = d.id
        JOIN breeds b ON d.breed_id = b.id
        WHERE se.created_at >= :period_start
        GROUP BY b.id, b.name
        ORDER BY entries_count DESC
        LIMIT 20
    """)
    result = await db.execute(sql, {"period_start": period_start})
    return [ShowAnalyticsRow.model_validate(dict(r)) for r in result.mappings().all()]
```

### 4. Зачем это нужно

ORM не справляется с такими запросами элегантно: подзапросы в SELECT, `FILTER (WHERE ...)`, `date_trunc` — всё это пишется на Raw SQL в одну строку и читается за секунду. ORM-эквивалент занял бы 30+ строк. `text()` с именованными параметрами — безопасная альтернатива f-строкам (SQL-инъекция невозможна).

### 5. Ключевые термины

- `text(sql)` — оборачивает строку в объект SQLAlchemy; поддерживает параметры
- `.mappings().all()` — результат как список словарей (ключ = имя колонки)
- `FILTER (WHERE ...)` — PostgreSQL: условная агрегация внутри `COUNT`, `SUM`
- `date_trunc('day', col)` — обрезать timestamp до дня (для группировки по дням)
- `NULLIF(expr, 0)` — вернуть NULL если 0; защита от деления на ноль в CTR

### 6. Как проверить

```bash
docker compose exec api python -c "
import asyncio
from app.database import async_session_maker
from app.repositories.analytics import get_platform_stats

async def test():
    async with async_session_maker() as db:
        stats = await get_platform_stats(db)
        print(stats.model_dump())

asyncio.run(test())
"
# {'total_users': N, 'verified_kennels': N, ...}
```

---

## Задача 5: `app/routers/admin/moderation.py` — эндпоинты модерации

### 1. Что делать

Создать директорию `app/routers/admin/` с файлом `__init__.py` и файл `app/routers/admin/moderation.py`. Зарегистрировать эндпоинты:

| Метод | Путь | Действие |
|-------|------|----------|
| GET | `/admin/moderation/classifieds` | Список на модерации |
| PUT | `/admin/moderation/classifieds/{id}` | Одобрить / отклонить |
| GET | `/admin/moderation/kennels` | Питомники на верификации |
| PUT | `/admin/moderation/kennels/{id}/verify` | Верифицировать |
| GET | `/admin/users` | Список пользователей |
| PUT | `/admin/users/{id}/block` | Блокировать / разблокировать |
| PUT | `/admin/users/{id}/role` | Сменить роль |

Все эндпоинты используют `Depends(require_admin)`.

### 2. Как это работает

Роутер — тонкий слой: принял запрос → делегировал в `moderation_service` → вернул схему. Никакой логики, только склейка HTTP-запроса с сервисом. `APIRouter(prefix="/admin", tags=["admin"])` группирует все эндпоинты под `/admin/...`.

### 3. API / примеры

```python
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.dependencies.admin import require_admin
from app.schemas.admin import ModerationAction, UserBlockAction, UserRoleUpdate
from app.services import moderation as mod_service

router = APIRouter(prefix="/admin", tags=["admin"])

@router.put("/moderation/classifieds/{classified_id}")
async def moderate_classified(
    classified_id: int,
    action: ModerationAction,
    db: AsyncSession = Depends(get_db),
    _: object = Depends(require_admin),
):
    if action.status == "approved":
        return await mod_service.approve_classified(db, classified_id)
    return await mod_service.reject_classified(db, classified_id, action.rejection_reason)

@router.put("/users/{user_id}/block")
async def block_user(
    user_id: int,
    action: UserBlockAction,
    db: AsyncSession = Depends(get_db),
    _: object = Depends(require_admin),
):
    return await mod_service.block_user(db, user_id, action.is_blocked)
```

Подключить в `app/main.py`:

```python
from app.routers.admin import moderation as admin_moderation
app.include_router(admin_moderation.router)
```

### 4. Зачем это нужно

`_: object = Depends(require_admin)` — стандартный приём, когда зависимость нужна только для side effect (проверки), а возвращаемое значение не используется. Имя `_` сигнализирует читателю: "нам нужен только эффект".

### 5. Ключевые термины

- `APIRouter(prefix=..., tags=[...])` — группа маршрутов с общим префиксом
- `_: object = Depends(...)` — зависимость ради side effect; результат игнорируется
- `include_router(router)` — подключить роутер к приложению

### 6. Как проверить

```bash
# В Swagger UI: http://localhost:8000/docs
# Должна появиться секция "admin" с 7 эндпоинтами

# Функциональная проверка:
curl http://localhost:8000/admin/moderation/classifieds \
  -H "Authorization: Bearer <admin_token>"
# → 200 список объявлений со статусом "pending"
```

---

## Задача 6: `app/routers/admin/analytics.py` — аналитические эндпоинты

### 1. Что делать

Создать файл `app/routers/admin/analytics.py` с эндпоинтами:

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/admin/dashboard` | Общая статистика |
| GET | `/admin/analytics/shows` | Топ пород по участиям |
| GET | `/admin/analytics/ads` | CTR/показы по дням |
| GET | `/admin/analytics/shows/{id}/report` | Сводка по выставке |

`/admin/analytics/shows` и `/admin/analytics/ads` принимают query-параметр `period_days: int = 30`. Маршрут `/admin/analytics/shows/{id}/report` доступен admin и organizer (добавить отдельную зависимость `require_admin_or_organizer`).

### 2. Как это работает

Query-параметры в FastAPI объявляются как аргументы функции без `Body()` — FastAPI автоматически читает их из URL (`?period_days=30`). `datetime.utcnow() - timedelta(days=period_days)` конвертирует `period_days` в `datetime` для SQL-запроса.

### 3. API / примеры

```python
from fastapi import APIRouter, Depends, Query
from datetime import datetime, timedelta, timezone
from app.database import get_db
from app.dependencies.admin import require_admin
from app.repositories import analytics as analytics_repo
from app.schemas.admin import PlatformStats, ShowAnalyticsRow, AdAnalyticsRow

router = APIRouter(prefix="/admin", tags=["admin-analytics"])

@router.get("/dashboard", response_model=PlatformStats)
async def dashboard(
    db=Depends(get_db),
    _=Depends(require_admin),
):
    return await analytics_repo.get_platform_stats(db)

@router.get("/analytics/shows", response_model=list[ShowAnalyticsRow])
async def show_analytics(
    period_days: int = Query(default=30, ge=1, le=365),
    db=Depends(get_db),
    _=Depends(require_admin),
):
    period_start = datetime.now(timezone.utc) - timedelta(days=period_days)
    return await analytics_repo.get_show_analytics(db, period_start)

@router.get("/analytics/shows/{show_id}/report")
async def show_report(
    show_id: int,
    db=Depends(get_db),
    _=Depends(require_admin),
):
    return await analytics_repo.get_show_report(db, show_id)
```

### 4. Зачем это нужно

`Query(ge=1, le=365)` — встроенная валидация: FastAPI вернёт 422 если передать `period_days=0` или `period_days=400`, не дойдя до SQL. `timezone.utc` в `datetime.now()` — явное указание временной зоны; без него сравнение с PostgreSQL `TIMESTAMPTZ` даст неверные результаты.

### 5. Ключевые термины

- `Query(default=..., ge=..., le=...)` — query-параметр с валидацией минимума/максимума
- `timedelta(days=n)` — смещение времени; `datetime.now() - timedelta(days=30)` = 30 дней назад
- `timezone.utc` — явная UTC-временная зона; избегает багов с локальным временем сервера
- 422 Unprocessable Entity — HTTP-код валидационной ошибки (FastAPI возвращает автоматически)

### 6. Как проверить

```bash
# Общая статистика
curl http://localhost:8000/admin/dashboard \
  -H "Authorization: Bearer <admin_token>"
# → {"total_users": N, "verified_kennels": N, ...}

# Аналитика выставок за 7 дней
curl "http://localhost:8000/admin/analytics/shows?period_days=7" \
  -H "Authorization: Bearer <admin_token>"
# → [{"breed": "Лабрадор", "entries_count": 12}, ...]

# Невалидный параметр
curl "http://localhost:8000/admin/analytics/shows?period_days=0" \
  -H "Authorization: Bearer <admin_token>"
# → 422 Unprocessable Entity

# Отчёт по выставке
curl http://localhost:8000/admin/analytics/shows/1/report \
  -H "Authorization: Bearer <admin_token>"
# → [{"breed": "...", "show_class": "...", "entries": N, ...}, ...]
```
