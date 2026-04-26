# SQLAlchemy ORM — Модели и колонки

## DeclarativeBase

`DeclarativeBase` (SQLAlchemy 2.0) — базовый класс, от которого наследуются все ORM-модели.

```python
from sqlalchemy.orm import DeclarativeBase

class Base(DeclarativeBase):
    pass
```

Все классы, унаследованные от `Base`, регистрируются в `Base.metadata`. Это позволяет Alembic автоматически генерировать миграции командой `alembic revision --autogenerate`.

## AsyncAttrs

Миксин `AsyncAttrs` добавляет поддержку `await` для ленивых relationship-атрибутов в async-контексте:

```python
from sqlalchemy.ext.asyncio import AsyncAttrs

class Base(AsyncAttrs, DeclarativeBase):
    pass
```

Без него обращение к `obj.relation` в async-коде вызовет ошибку.

## Mapped и mapped_column

`Mapped[T]` — type hint, который сообщает ORM тип Python-значения колонки.  
`mapped_column(...)` — объявляет колонку с параметрами.

```python
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import String

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    is_active: Mapped[bool] = mapped_column(default=True)
```

`Mapped[str]` означает NOT NULL. `Mapped[str | None]` или `Mapped[Optional[str]]` — допускает NULL.

## TimestampMixin

Миксин — обычный Python-класс с колонками, добавляемый через множественное наследование:

```python
from datetime import datetime
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import func

class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
```

- `server_default=func.now()` — PostgreSQL сам выставляет `NOW()` при INSERT, Python-код значение не передаёт
- `onupdate=func.now()` — SQLAlchemy обновляет поле при каждом UPDATE через ORM

## __tablename__

Обязательный атрибут модели — имя таблицы в БД:

```python
class User(Base, TimestampMixin):
    __tablename__ = "users"
```

## Enum в PostgreSQL через SQLAlchemy

```python
import enum
from sqlalchemy import Enum as SAEnum

class UserRole(enum.Enum):
    user = "user"
    admin = "admin"

class User(Base):
    role: Mapped[UserRole] = mapped_column(SAEnum(UserRole), default=UserRole.user)
```

PostgreSQL создаёт нативный тип `ENUM` — хранится эффективно, защищена от невалидных значений на уровне БД.

## relationship — связи между моделями

`relationship()` определяет связь на уровне ORM (не на уровне БД). FK определяет связь в БД, relationship — в Python-коде.

```python
from sqlalchemy.orm import relationship

class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    roles: Mapped[list["UserRole"]] = relationship(back_populates="user")

class UserRole(Base):
    __tablename__ = "user_roles"
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    user: Mapped["User"] = relationship(back_populates="roles")
```

- Первый аргумент — **целевой класс** (строка или класс). Не `ForeignKey`, не колонка.
- `back_populates` — связывает оба направления: `user.roles` и `role.user`.
- Если у модели **два FK на одну таблицу**, SQLAlchemy бросит `AmbiguousForeignKeysError`. Нужно явно указать `foreign_keys`:

```python
class UserRole(Base):
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    granted_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))

class User(Base):
    # Явно указываем, по какому FK строить связь
    roles: Mapped[list["UserRole"]] = relationship(
        foreign_keys="UserRole.user_id", back_populates="user"
    )
```

## Nullable — Mapped[T] vs Mapped[T | None]

В SQLAlchemy 2.0 nullable определяется **аннотацией**, а не параметром `nullable=`:

```python
# NOT NULL — обязательное поле
email: Mapped[str] = mapped_column(String(255))

# NULL допустим — необязательное поле
avatar_url: Mapped[str | None] = mapped_column(String(255))
```

Типичная ошибка — противоречие аннотации и параметра:

```python
# НЕПРАВИЛЬНО: Mapped[str] = NOT NULL, но default=None пытается записать NULL
avatar: Mapped[str] = mapped_column(default=None)

# НЕПРАВИЛЬНО: Mapped[uuid.UUID] = NOT NULL, но nullable=True разрешает NULL
granted_by: Mapped[uuid.UUID] = mapped_column(nullable=True)

# ПРАВИЛЬНО: аннотация и поведение совпадают
avatar: Mapped[str | None] = mapped_column(String(255))
granted_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
```

## server_default vs default vs onupdate

Три разных механизма установки значений:

| Параметр | Кто выполняет | Когда |
|---|---|---|
| `default=` | Python (SQLAlchemy) | При INSERT, до отправки в БД |
| `server_default=` | PostgreSQL | При INSERT, на стороне БД |
| `onupdate=` | Python (SQLAlchemy) | При UPDATE через ORM |

```python
# server_default — БД сама ставит NOW() при INSERT
created_at: Mapped[datetime] = mapped_column(server_default=func.now())

# onupdate — SQLAlchemy обновляет при каждом UPDATE через ORM
updated_at: Mapped[datetime] = mapped_column(
    server_default=func.now(), onupdate=func.now()
)
```

Когда **не** использовать `onupdate`:
- `granted_at` — дата выдачи роли фиксируется один раз
- `expires_at` — срок истечения не должен сбрасываться при обновлении строки

Когда **не** использовать `server_default=func.now()`:
- `expires_at` — токен, который истекает в момент создания, бесполезен
- `used_at` — поле «когда использовано» должно быть NULL до использования

## Mapped[T] ожидает Python-тип, не SQLAlchemy-тип

```python
# НЕПРАВИЛЬНО: String — это SQLAlchemy column type
token_hash: Mapped[String]

# ПРАВИЛЬНО: str — Python-тип, String(255) — в mapped_column
token_hash: Mapped[str] = mapped_column(String(255))
```

`Mapped[T]` описывает тип **Python-значения** атрибута. SQLAlchemy-типы (`String`, `Integer`, `Boolean`) — это типы **колонок в БД**, они передаются в `mapped_column()`.

## Ссылки

- [SQLAlchemy 2.0 Mapped Column Docs](https://docs.sqlalchemy.org/en/20/orm/mapped_attributes.html)
- [DeclarativeBase](https://docs.sqlalchemy.org/en/20/orm/declarative_styles.html)
- [relationship()](https://docs.sqlalchemy.org/en/20/orm/relationships.html)
- [Column INSERT/UPDATE Defaults](https://docs.sqlalchemy.org/en/20/core/defaults.html)
