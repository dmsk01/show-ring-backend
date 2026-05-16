# Этап 3: Справочники — задачи

> Цель: модели + миграция + CRUD + seed-данные для справочных таблиц
> (породы, классы выставок, ранги, титулы, оценки).

---

## Задача 3.1 — ORM-модели справочников

### 1. Что делать

Файл: `app/models/reference.py`

Создать SQLAlchemy-модели для следующих таблиц:

- `AnimalType` → таблица `animal_types` (id, code, name)
- `BreedGroup` → таблица `breed_groups` (id, fci_number, name, animal_type_id FK)
- `Breed` → таблица `breeds` (id, name, name_en, fci_number, group_id FK, animal_type_id FK)
- `ShowClass` → таблица `show_classes` (id, code, name, age_from_months, age_to_months nullable, can_receive_cac)
- `ShowRank` → таблица `show_ranks` (id, code, name, priority)
- `Title` → таблица `titles` (id, code, name, description nullable)
- `Grade` → таблица `grades` (id, code, name, for_puppies bool)

### 2. Как это работает

Каждая модель наследует `Base` из `app.models.base`. Поля описываются
через `Mapped[тип]` + `mapped_column(...)` — это SQLAlchemy 2.0 typed
API. Связи между таблицами описываются через `ForeignKey` в
`mapped_column` и `relationship` на уровне Python-объекта.

`nullable` поле описывается как `Mapped[тип | None]` — SQLAlchemy
автоматически делает колонку nullable.

### 3. API технологии / примеры

```python
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy import String, Boolean, Integer, ForeignKey
from app.models.base import Base

class AnimalType(Base):
    __tablename__ = "animal_types"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    breeds: Mapped[list["Breed"]] = relationship(back_populates="animal_type")

class Breed(Base):
    __tablename__ = "breeds"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    group_id: Mapped[int | None] = mapped_column(ForeignKey("breed_groups.id"))
    animal_type_id: Mapped[int] = mapped_column(ForeignKey("animal_types.id"))
    animal_type: Mapped["AnimalType"] = relationship(back_populates="breeds")
```

### 4. Зачем это нужно

Справочники — фундамент для всей бизнес-логики: выставка привязана к
`ShowClass`, собака — к `Breed`. Без этих таблиц невозможно создать
ни питомник, ни собаку, ни выставочную запись. Заводятся один раз,
потом только читаются (иногда пополняются).

### 5. Ключевые термины / функции

- `Mapped[тип]` — типизированное поле SQLAlchemy 2.0
- `mapped_column(ForeignKey(...))` — колонка с внешним ключом
- `relationship(back_populates=...)` — двусторонняя ORM-связь
- `Mapped[тип | None]` — nullable колонка (Python `Optional`)
- `unique=True` — уникальный индекс на колонке

### 6. Как проверить

```python
# Проверить что модели импортируются без ошибок
python -c "from app.models.reference import Breed, ShowClass, Grade; print('OK')"
```

Если вывод `OK` — импорт прошёл. Ошибки типа `ImportError` или
`InvalidRequestError` означают проблему в определении моделей.

---

## Задача 3.2 — Миграция Alembic для справочников

### 1. Что делать

Сгенерировать и применить миграцию для всех таблиц из задачи 3.1.

Шаги:
1. Добавить импорт `reference` в `app/models/__init__.py` (чтобы Alembic видел модели)
2. `alembic revision --autogenerate -m "stage_03_references"`
3. Проверить сгенерированный файл в `migrations/versions/`
4. `alembic upgrade head`

### 2. Как это работает

`--autogenerate` сравнивает состояние SQLAlchemy-моделей (`Base.metadata`)
с реальной схемой БД и генерирует `upgrade()` / `downgrade()`.
Чтобы Alembic "увидел" новые модели, они должны быть импортированы до
вызова `Base.metadata` — обычно через `app/models/__init__.py`.

Без явного импорта Alembic не обнаружит таблицы и сгенерирует пустую
миграцию.

### 3. API технологии / примеры

```python
# app/models/__init__.py — добавить строку:
from app.models import reference  # noqa: F401

# Терминал:
alembic revision --autogenerate -m "stage_03_references"
# Создаст файл вида: migrations/versions/abc123_stage_03_references.py

alembic upgrade head
# Применит миграцию к БД
```

Проверить сгенерированный файл: в `upgrade()` должны быть вызовы
`op.create_table(...)` для каждой из 7 новых таблиц.

### 4. Зачем это нужно

Миграция — единственный надёжный способ синхронизировать схему БД
с кодом. Без неё таблицы существуют только в Python-объектах, но не
в PostgreSQL. Alembic хранит историю изменений, позволяя откатиться
до любого состояния командой `downgrade`.

### 5. Ключевые термины / функции

- `alembic revision --autogenerate` — сгенерировать миграцию по diff
- `alembic upgrade head` — применить все pending-миграции
- `alembic downgrade -1` — откатить последнюю миграцию
- `op.create_table(...)` — DDL-операция создания таблицы
- `Base.metadata` — реестр всех SQLAlchemy-моделей

### 6. Как проверить

```bash
alembic current
```

Вывод должен содержать хэш последней применённой миграции с пометкой
`(head)`. Затем в psql:

```sql
\dt
```

В списке должны появиться `animal_types`, `breeds`, `breed_groups`,
`show_classes`, `show_ranks`, `titles`, `grades`.

---

## Задача 3.3 — Схемы и репозиторий

### 1. Что делать

**Файл `app/schemas/reference.py`** — Pydantic-схемы для чтения:
- `AnimalTypeResponse`, `BreedGroupResponse`, `BreedResponse`
- `ShowClassResponse`, `ShowRankResponse`, `TitleResponse`, `GradeResponse`
- `BreedListResponse` с пагинацией: `items: list[BreedResponse]`, `total: int`, `page: int`, `per_page: int`

**Файл `app/repositories/reference.py`** — async-функции запросов:
- `get_breeds(db, animal_type, group_id, page, per_page)` → `(list[Breed], int)`
- `get_breed_by_id(db, breed_id)` → `Breed | None`
- `get_all_show_classes(db)` → `list[ShowClass]`
- `get_all_titles(db)` → `list[Title]`
- `get_all_grades(db)` → `list[Grade]`
- `create_breed(db, **fields)` → `Breed`
- `delete_breed(db, breed_id)` → `bool` (False если есть зависимые записи)

### 2. Как это работает

Схемы используют `model_config = ConfigDict(from_attributes=True)` для
создания из ORM-объектов через `BreedResponse.model_validate(orm_obj)`.

Пагинация реализуется через `SELECT ... LIMIT :limit OFFSET :offset` и
отдельный `SELECT count(*)` для поля `total`.

Защита от удаления через `EXISTS`-подзапрос: перед `DELETE FROM breeds`
проверяем, нет ли строк в `dogs` с `breed_id = :id`.

### 3. API технологии / примеры

```python
# Пагинация через SQLAlchemy
from sqlalchemy import select, func

stmt = select(Breed).where(Breed.animal_type_id == animal_type_id)
count_stmt = select(func.count()).select_from(stmt.subquery())
total = (await db.execute(count_stmt)).scalar()

stmt = stmt.limit(per_page).offset((page - 1) * per_page)
breeds = (await db.execute(stmt)).scalars().all()

# Защита от удаления — проверка EXISTS
from sqlalchemy import exists
has_dogs = await db.scalar(select(exists().where(Dog.breed_id == breed_id)))
if has_dogs:
    return False  # нельзя удалить
```

### 4. Зачем это нужно

Репозиторий изолирует SQL от бизнес-логики: роутер не знает, как
устроены запросы к БД, он только вызывает `get_breeds(...)`. Это
позволяет менять SQL без изменения роутеров и тестировать запросы
отдельно. `delete_breed` возвращает `bool`, а не кидает исключение —
решение принимает вызывающий код (сервис или роутер).

### 5. Ключевые термины / функции

- `ConfigDict(from_attributes=True)` — позволяет `model_validate(orm_obj)`
- `func.count()` — SQL `COUNT(*)` через SQLAlchemy
- `select_from(subquery)` — `COUNT` по уже отфильтрованным строкам
- `scalars().all()` — извлечь список ORM-объектов из результата
- `exists().where(...)` — SQL `EXISTS (SELECT 1 WHERE ...)`

### 6. Как проверить

```python
python -c "
import asyncio
from app.schemas.reference import BreedResponse, BreedListResponse
print(BreedListResponse.model_fields.keys())
"
```

Вывод должен содержать `items`, `total`, `page`, `per_page`.

---

## Задача 3.4 — Публичные GET-эндпоинты

### 1. Что делать

Файл: `app/routers/references.py`

Реализовать публичные (без авторизации) роутеры:

```
GET /breeds?animal_type=dog&group=1&page=1&per_page=50
GET /breeds/{id}
GET /show-classes
GET /show-ranks
GET /titles
GET /grades
```

Подключить роутер в `app/main.py`.

### 2. Как это работает

Query-параметры FastAPI объявляются как аргументы функции с дефолтными
значениями. Параметр `page: int = 1` → FastAPI автоматически принимает
`?page=2` и валидирует тип.

`Optional` параметры (фильтры) объявляются как `animal_type: str | None = None`.
В репозитории `WHERE` добавляется только если параметр передан.

### 3. API технологии / примеры

```python
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db

router = APIRouter(tags=["references"])

@router.get("/breeds")
async def list_breeds(
    animal_type: str | None = Query(None),
    group: int | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    breeds, total = await get_breeds(db, animal_type, group, page, per_page)
    return BreedListResponse(items=breeds, total=total, page=page, per_page=per_page)
```

### 4. Зачем это нужно

Публичные справочники нужны клиентскому приложению: форма регистрации
собаки показывает список пород, форма заявки на выставку — список
классов. Без этих эндпоинтов фронтенд не может заполнить выпадающие
списки. Они публичные — авторизация излишня (данные не секретные).

### 5. Ключевые термины / функции

- `Query(default, ge=N, le=M)` — параметр запроса с валидацией диапазона
- `tags=["references"]` — группировка эндпоинтов в Swagger
- `ge=1` / `le=200` — `greater_or_equal` / `less_or_equal` ограничения

### 6. Как проверить

```bash
curl "http://localhost:8000/breeds?animal_type=dog&per_page=5"
```

Ответ: JSON с полями `items` (массив пород), `total`, `page`, `per_page`.

```bash
curl "http://localhost:8000/show-classes"
```

Ответ: массив классов с полями `code`, `name`, `age_from_months`, `can_receive_cac`.

---

## Задача 3.5 — Admin CRUD и seed-скрипт

### 1. Что делать

**Файл `app/routers/admin/references.py`** — CRUD только для admin-роли:

```
POST   /admin/breeds
PUT    /admin/breeds/{id}
DELETE /admin/breeds/{id}
```

Аналогично для `show_classes`, `titles`, `grades` (по необходимости).

Защита через `Depends(require_any_role("admin"))`.

**Файл `scripts/seed_references.py`** — скрипт заполнения начальными
данными: animal_types (dog, cat), show_classes (8 классов из плана),
show_ranks (CACIB, CAC и др.), grades, несколько пород для теста.

### 2. Как это работает

Admin-роутер использует ту же зависимость `require_any_role`, что и
обычные защищённые эндпоинты. Разница — передаётся строка `"admin"`.
FastAPI проверит роли пользователя из JWT и вернёт 403, если роль
не совпадает.

Seed-скрипт — обычный Python-файл, запускаемый напрямую. Он создаёт
AsyncSession и вставляет данные через `session.add()` + `commit`.
`ON CONFLICT DO NOTHING` (через `insert(...).on_conflict_do_nothing()`)
позволяет запускать скрипт повторно без дублирования.

### 3. API технологии / примеры

```python
# Admin роутер
from app.dependencies import require_any_role

router = APIRouter(prefix="/admin", tags=["admin"])

@router.post("/breeds")
async def create_breed(
    body: BreedCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_any_role("admin")),
):
    breed = await repo.create_breed(db, **body.model_dump())
    await db.commit()
    return BreedResponse.model_validate(breed)

# Seed-скрипт (insert без дублей)
from sqlalchemy.dialects.postgresql import insert

stmt = insert(AnimalType).values(code="dog", name="Собаки")
stmt = stmt.on_conflict_do_nothing(index_elements=["code"])
await session.execute(stmt)
```

### 4. Зачем это нужно

Admin CRUD позволяет добавлять новые породы и классы без изменения кода.
Seed-скрипт — одноразовая процедура инициализации: новый разработчик
клонирует репозиторий, запускает скрипт и получает рабочую БД со всеми
справочниками. Без seed данных тестирование выставок и пород невозможно.

### 5. Ключевые термины / функции

- `require_any_role("admin")` — зависимость-защитник, 403 если не admin
- `on_conflict_do_nothing(index_elements=[...])` — INSERT IGNORE аналог
- `session.add(obj)` — добавить объект в pending-состояние сессии
- `await session.commit()` — зафиксировать все pending изменения в БД
- `asyncio.run(main())` — запустить async-функцию из синхронного скрипта

### 6. Как проверить

```bash
# Запустить seed
python scripts/seed_references.py

# Проверить данные в БД
# (в psql или через GET-эндпоинты)
curl "http://localhost:8000/show-classes"
# Должно вернуть 8 классов: baby, puppy, junior, ...

# Попытка создать породу без токена → 401
curl -X POST http://localhost:8000/admin/breeds \
  -H "Content-Type: application/json" \
  -d '{"name":"Овчарка","animal_type_id":1}'

# С токеном обычного пользователя → 403
# С токеном admin → 201
```
