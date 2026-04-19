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

## Ссылки

- [SQLAlchemy 2.0 Mapped Column Docs](https://docs.sqlalchemy.org/en/20/orm/mapped_attributes.html)
- [DeclarativeBase](https://docs.sqlalchemy.org/en/20/orm/declarative_styles.html)
