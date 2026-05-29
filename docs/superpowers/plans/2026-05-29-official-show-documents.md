# Официальные документы выставки (формат РКФ) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Генерировать диплом, ринговую ведомость и каталог выставки в точном формате/оформлении образцов РКФ, отдавая редактируемый DOCX и/или PDF, не трогая существующую подсистему генерации (этап 8).

**Architecture:** DOCX-шаблоны (созданы из RTF-образцов, размечены плейсхолдерами `docxtpl`) лежат в `app/templates/documents/`. Новые «билдеры контекста» собирают данные из БД и формируют словарь; новый модуль `app/utils/docx_render.py` рендерит шаблон через `docxtpl` и при необходимости конвертирует в PDF через LibreOffice headless. Воркер документов получает новые типы задач, новые ручки — рядом со старыми. Для ФИО добавляется `UserProfile`, для заводчика — поля на `Dog`.

**Tech Stack:** FastAPI, SQLAlchemy 2.0 async, Alembic, RabbitMQ (aio-pika), MinIO, `docxtpl` (+ `python-docx`, `jinja2`), LibreOffice (`soffice --headless`), pytest + pytest-asyncio.

---

## Важно: что НЕ трогаем

- `app/utils/pdf.py`, `render_catalog`/`render_diploma*` — без изменений.
- Существующие `DocumentKind.CATALOG/DIPLOMA/DIPLOMAS_BATCH`, их ветки в
  `worker/handlers/document_handler.py` и эндпоинты в
  `app/routers/documents.py` — без изменений (только дополняем рядом).
- `_user_display` в `app/services/document.py` оставляем как есть (старые
  PDF продолжают печатать email). Официальные документы используют новый
  резолвер имён.

## Структура файлов

**Создаём:**
- `app/models/user.py` → класс `UserProfile` (в существующем файле).
- `app/utils/names.py` — чистые хелперы `full_name`, `judge_display`.
- `app/utils/docx_render.py` — рендер docxtpl + конвертация в PDF.
- `app/services/document_official.py` — билдеры контекста (DB + чистые
  шейперы) для 3 документов + readiness.
- `app/templates/documents/diploma.docx` — шаблон диплома.
- `app/templates/documents/ring_sheet.docx` — шаблон ведомости.
- `app/templates/documents/catalog.docx` — шаблон каталога.
- `migrations/versions/<auto>_official_documents.py` — миграция.
- Тесты: `tests/unit/test_names.py`, `tests/unit/test_official_context.py`,
  `tests/unit/test_docx_render.py`, `tests/unit/test_official_templates.py`.

**Модифицируем:**
- `app/models/dog.py` — поля `breeder_kennel_id`, `breeder_name` + relationship.
- `app/schemas/user.py` — схемы профиля.
- `app/repositories/user.py` — get/update профиля.
- `app/routers/users.py` — ручки профиля.
- `app/schemas/dog.py` — поля заводчика в Create/Update/Response (если есть).
- `app/schemas/task.py` — новые значения `DocumentKind`.
- `worker/handlers/document_handler.py` — новые ветки + `extension` в `_upload_and_register`.
- `app/routers/documents.py` — новые официальные эндпоинты + `/context` + `/readiness`.
- `requirements.txt` — `docxtpl`.

---

## Фаза 0. Зависимости и конвертер

### Task 1: Добавить зависимость docxtpl

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Дописать зависимость**

В `requirements.txt` в секцию «Этап 8» под строкой `reportlab>=4.2.0` добавить:

```
docxtpl>=0.18.0                # DOCX-шаблоны с Jinja-плейсхолдерами (официальные документы РКФ)
```

- [ ] **Step 2: Установить**

Run: `.\venv\Scripts\python.exe -m pip install "docxtpl>=0.18.0"`
Expected: установка `docxtpl`, `python-docx`, `lxml` без ошибок; в конце `Successfully installed ... docxtpl-...`.

- [ ] **Step 3: Проверить импорт**

Run: `.\venv\Scripts\python.exe -c "import docxtpl; print(docxtpl.__version__)"`
Expected: печатает версию (например `0.18.0`).

- [ ] **Step 4: Commit**

```bash
git add requirements.txt
git commit -m "build: add docxtpl for official RKF document templates"
```

---

### Task 2: Модуль рендера DOCX и конвертации в PDF

**Files:**
- Create: `app/utils/docx_render.py`
- Test: `tests/unit/test_docx_render.py`

- [ ] **Step 1: Написать падающий тест на выбор soffice и ошибку при отсутствии**

```python
# tests/unit/test_docx_render.py
import builtins
import pytest

from app.utils import docx_render


def test_find_soffice_returns_none_when_absent(monkeypatch):
    monkeypatch.setattr(docx_render.shutil, "which", lambda name: None)
    monkeypatch.setattr(docx_render.Path, "exists", lambda self: False)
    assert docx_render._find_soffice() is None


def test_convert_raises_when_soffice_missing(monkeypatch):
    monkeypatch.setattr(docx_render, "_find_soffice", lambda: None)
    with pytest.raises(docx_render.PdfConversionError):
        docx_render.convert_docx_to_pdf(b"PK\x03\x04 fake docx")
```

- [ ] **Step 2: Запустить — упадёт на отсутствии модуля**

Run: `.\venv\Scripts\python.exe -m pytest tests/unit/test_docx_render.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.utils.docx_render'`.

- [ ] **Step 3: Реализовать модуль**

```python
# app/utils/docx_render.py
"""
Рендер официальных документов из DOCX-шаблонов и конвертация в PDF.

Поток: docxtpl подставляет данные в .docx-шаблон → bytes. Если нужен PDF —
конвертируем тот же .docx через LibreOffice headless (soffice).

Почему LibreOffice, а не сборка PDF в коде: документы РКФ имеют сложное
фиксированное оформление (рамки, шрифты, двуязычные блоки). Шаблон в Word
сохраняет его 1-в-1; повторять это программно — дорого и неточно.

soffice — блокирующий subprocess. В асинхронном воркере вызывать через
asyncio.to_thread (см. document_handler).
"""

from __future__ import annotations

import io
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from docxtpl import DocxTemplate

logger = logging.getLogger(__name__)

# app/utils/docx_render.py -> app/ -> app/templates/documents
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates" / "documents"

DOCX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


class PdfConversionError(RuntimeError):
    """LibreOffice недоступен или конвертация завершилась ошибкой."""


def render_docx(template_name: str, context: dict) -> bytes:
    """
    Подставляет context в шаблон templates/documents/<template_name>
    и возвращает байты готового .docx.
    """
    tpl = DocxTemplate(str(TEMPLATES_DIR / template_name))
    tpl.render(context)
    buf = io.BytesIO()
    tpl.save(buf)
    return buf.getvalue()


def _find_soffice() -> str | None:
    """Путь к LibreOffice/soffice или None, если не найден."""
    for name in ("soffice", "libreoffice"):
        path = shutil.which(name)
        if path:
            return path
    # Типовой путь установки на Windows.
    win = Path(r"C:/Program Files/LibreOffice/program/soffice.exe")
    if win.exists():
        return str(win)
    return None


def convert_docx_to_pdf(docx_bytes: bytes, *, timeout: int = 120) -> bytes:
    """
    Конвертирует .docx (байты) в PDF (байты) через LibreOffice headless.
    Бросает PdfConversionError, если soffice не найден/упал.
    """
    soffice = _find_soffice()
    if soffice is None:
        raise PdfConversionError("LibreOffice (soffice) not found on PATH")
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        src = tmpdir / "in.docx"
        src.write_bytes(docx_bytes)
        try:
            proc = subprocess.run(
                [
                    soffice,
                    "--headless",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    str(tmpdir),
                    str(src),
                ],
                capture_output=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            raise PdfConversionError(f"soffice timeout after {timeout}s") from e
        out = tmpdir / "in.pdf"
        if proc.returncode != 0 or not out.exists():
            err = proc.stderr.decode(errors="ignore")[:500]
            raise PdfConversionError(
                f"soffice failed: rc={proc.returncode} err={err}"
            )
        return out.read_bytes()
```

- [ ] **Step 4: Запустить тесты — должны пройти**

Run: `.\venv\Scripts\python.exe -m pytest tests/unit/test_docx_render.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add app/utils/docx_render.py tests/unit/test_docx_render.py
git commit -m "feat(docs): docxtpl render + LibreOffice PDF conversion utility"
```

---

## Фаза 1. Модель данных: профиль и заводчик

### Task 3: Модель UserProfile

**Files:**
- Modify: `app/models/user.py`

- [ ] **Step 1: Добавить relationship в `User`**

В классе `User` (после блока `roles`/`refresh_tokens`/`email_verification_tokens` relationships, перед закрытием класса) добавить:

```python
    profile: Mapped["UserProfile | None"] = relationship(
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
    )
```

- [ ] **Step 2: Добавить класс `UserProfile`**

В конце файла `app/models/user.py` добавить:

```python
class UserProfile(Base, TimestampMixin):
    """
    Профиль пользователя с человекочитаемыми данными (ФИО, страна).

    Вынесен в отдельную таблицу 1:1, чтобы не раздувать users (модель
    аутентификации) и заполнять опционально. Нужен для официальных
    документов: ФИО владельца/заводчика/эксперта и страна эксперта.
    """

    __tablename__ = "user_profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    last_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    patronymic: Mapped[str | None] = mapped_column(String(128), nullable=True)
    country: Mapped[str | None] = mapped_column(String(64), nullable=True)

    user: Mapped["User"] = relationship(back_populates="profile")
```

Проверить, что в начале файла уже импортированы `String`, `ForeignKey`,
`UUID`, `Mapped`, `mapped_column`, `relationship`, `TimestampMixin`, `uuid`
(они используются другими моделями файла — `relationship` уже импортирован,
`String`/`ForeignKey`/`UUID` тоже; `TimestampMixin` — добавить в импорт из
`app.models.base`, сейчас импортируется только `Base`).

В строке импорта `from app.models.base import Base` заменить на:

```python
from app.models.base import Base, TimestampMixin
```

- [ ] **Step 3: Проверить, что модуль импортируется**

Run: `.\venv\Scripts\python.exe -c "from app.models.user import UserProfile; print(UserProfile.__tablename__)"`
Expected: печатает `user_profiles`.

- [ ] **Step 4: Commit**

```bash
git add app/models/user.py
git commit -m "feat(model): UserProfile (ФИО/страна) 1:1 to User"
```

---

### Task 4: Поля заводчика на Dog

**Files:**
- Modify: `app/models/dog.py`

- [ ] **Step 1: Добавить колонки в `Dog`**

В классе `Dog`, сразу после поля `microchip` (строка с `microchip: Mapped[...]`)
добавить:

```python
    # Питомник-заводчик: где собака рождена. Отличается от kennel_id
    # (текущий питомник владельца), не меняется при продаже. Источник
    # графы «Заводчик»/«Питомник» в документах.
    breeder_kennel_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("kennels.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Free-text заводчик для собак, рождённых вне платформы (импорт):
    # когда breeder_kennel_id неизвестен.
    breeder_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
```

- [ ] **Step 2: Добавить relationship**

В блоке relationships класса `Dog` (рядом с `kennel: Mapped[...]`) добавить:

```python
    breeder_kennel: Mapped["Kennel | None"] = relationship(  # noqa: F821
        foreign_keys=[breeder_kennel_id]
    )
```

Поскольку теперь у `Dog` два FK на `kennels` (`kennel_id` и
`breeder_kennel_id`), у существующего relationship `kennel` указать
`foreign_keys`, чтобы SQLAlchemy не запутался. Заменить строку:

```python
    kennel: Mapped["Kennel | None"] = relationship(back_populates="dogs")  # noqa: F821
```

на:

```python
    kennel: Mapped["Kennel | None"] = relationship(
        back_populates="dogs", foreign_keys=[kennel_id]
    )  # noqa: F821
```

- [ ] **Step 3: Проверить импорт моделей**

Run: `.\venv\Scripts\python.exe -c "from app.models.dog import Dog; print(Dog.breeder_kennel_id, Dog.breeder_name)"`
Expected: печатает два дескриптора колонок без ошибок (нет `AmbiguousForeignKeysError`).

- [ ] **Step 4: Commit**

```bash
git add app/models/dog.py
git commit -m "feat(model): Dog.breeder_kennel_id + breeder_name (заводчик != владелец)"
```

---

### Task 5: Миграция Alembic

**Files:**
- Create: `migrations/versions/<auto>_official_documents.py`

- [ ] **Step 1: Сгенерировать пустую ревизию (auto down_revision)**

Run: `.\venv\Scripts\python.exe -m alembic revision -m "official documents: user_profiles + dog breeder"`
Expected: создан файл в `migrations/versions/`, в нём `down_revision` уже
указывает на текущий head. Если БД недоступна — это не мешает: ревизия
создаётся из файлов. Проверить head: `.\venv\Scripts\python.exe -m alembic heads`.

- [ ] **Step 2: Заполнить upgrade/downgrade**

В созданном файле заменить тела функций на:

```python
def upgrade() -> None:
    op.create_table(
        "user_profiles",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("last_name", sa.String(length=128), nullable=True),
        sa.Column("first_name", sa.String(length=128), nullable=True),
        sa.Column("patronymic", sa.String(length=128), nullable=True),
        sa.Column("country", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.add_column(
        "dogs", sa.Column("breeder_kennel_id", sa.UUID(), nullable=True)
    )
    op.add_column(
        "dogs", sa.Column("breeder_name", sa.String(length=255), nullable=True)
    )
    op.create_index(
        op.f("ix_dogs_breeder_kennel_id"),
        "dogs",
        ["breeder_kennel_id"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_dogs_breeder_kennel_id_kennels",
        "dogs",
        "kennels",
        ["breeder_kennel_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_dogs_breeder_kennel_id_kennels", "dogs", type_="foreignkey"
    )
    op.drop_index(op.f("ix_dogs_breeder_kennel_id"), table_name="dogs")
    op.drop_column("dogs", "breeder_name")
    op.drop_column("dogs", "breeder_kennel_id")
    op.drop_table("user_profiles")
```

Убедиться, что вверху файла есть `import sqlalchemy as sa` и `from alembic import op` (Alembic кладёт их в шаблон автоматически).

- [ ] **Step 3: Применить миграцию (если БД поднята)**

Run: `.\venv\Scripts\python.exe -m alembic upgrade head`
Expected: `Running upgrade ... -> <rev>, official documents...`. Если БД не
поднята локально — отметить шаг как отложенный и применить при запущенном
PostgreSQL (см. CLAUDE/память про порт 5432).

- [ ] **Step 4: Проверить откат не ломается (если БД поднята)**

Run: `.\venv\Scripts\python.exe -m alembic downgrade -1` затем `.\venv\Scripts\python.exe -m alembic upgrade head`
Expected: обе команды успешны.

- [ ] **Step 5: Commit**

```bash
git add migrations/versions/
git commit -m "migrate: user_profiles + dogs.breeder_kennel_id/breeder_name"
```

---

### Task 6: Чистые хелперы имён

**Files:**
- Create: `app/utils/names.py`
- Test: `tests/unit/test_names.py`

- [ ] **Step 1: Написать падающий тест**

```python
# tests/unit/test_names.py
from types import SimpleNamespace

from app.utils.names import full_name, judge_display


def _user(email, profile=None):
    return SimpleNamespace(email=email, profile=profile)


def _profile(last=None, first=None, patr=None, country=None):
    return SimpleNamespace(
        last_name=last, first_name=first, patronymic=patr, country=country
    )


def test_full_name_all_parts():
    u = _user("a@b.c", _profile("Иванов", "Иван", "Иванович"))
    assert full_name(u) == "Иванов Иван Иванович"


def test_full_name_partial_skips_empty():
    u = _user("a@b.c", _profile("Иванов", "Иван", None))
    assert full_name(u) == "Иванов Иван"


def test_full_name_falls_back_to_email_when_no_profile():
    assert full_name(_user("a@b.c", None)) == "a@b.c"


def test_full_name_falls_back_when_profile_empty():
    assert full_name(_user("a@b.c", _profile())) == "a@b.c"


def test_full_name_none_user_returns_empty():
    assert full_name(None) == ""


def test_judge_display_with_country():
    u = _user("j@b.c", _profile("Никитина", "Ольга", country="Россия"))
    assert judge_display(u) == "Никитина Ольга (Россия)"


def test_judge_display_without_country():
    u = _user("j@b.c", _profile("Никитина", "Ольга"))
    assert judge_display(u) == "Никитина Ольга"
```

- [ ] **Step 2: Запустить — упадёт (нет модуля)**

Run: `.\venv\Scripts\python.exe -m pytest tests/unit/test_names.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.utils.names'`.

- [ ] **Step 3: Реализовать**

```python
# app/utils/names.py
"""
Чистые хелперы для человекочитаемых имён в официальных документах.

Принимают объект с .email и (опционально) .profile с полями last_name/
first_name/patronymic/country. Не делают запросов в БД — вызывающий код
обязан загрузить profile заранее (через selectinload или awaitable_attrs).
"""

from __future__ import annotations

from typing import Any


def full_name(user: Any | None) -> str:
    """«Фамилия Имя Отчество», пустые части опускаются. Если профиль пуст —
    fallback на email. None → пустая строка."""
    if user is None:
        return ""
    profile = getattr(user, "profile", None)
    if profile is not None:
        parts = [
            getattr(profile, "last_name", None),
            getattr(profile, "first_name", None),
            getattr(profile, "patronymic", None),
        ]
        joined = " ".join(p.strip() for p in parts if p and p.strip())
        if joined:
            return joined
    return getattr(user, "email", "") or ""


def judge_display(user: Any | None) -> str:
    """«Фамилия Имя Отчество (Страна)». Без страны — только ФИО."""
    name = full_name(user)
    profile = getattr(user, "profile", None)
    country = getattr(profile, "country", None) if profile is not None else None
    if country and country.strip():
        return f"{name} ({country.strip()})"
    return name
```

- [ ] **Step 4: Запустить — пройдёт**

Run: `.\venv\Scripts\python.exe -m pytest tests/unit/test_names.py -v`
Expected: PASS (7 passed).

- [ ] **Step 5: Commit**

```bash
git add app/utils/names.py tests/unit/test_names.py
git commit -m "feat(docs): pure name helpers full_name/judge_display"
```

---

### Task 7: Схемы и репозиторий профиля

**Files:**
- Modify: `app/schemas/user.py`
- Modify: `app/repositories/user.py`

- [ ] **Step 1: Добавить схемы профиля**

В конец `app/schemas/user.py` добавить:

```python
class UserProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    last_name: str | None = None
    first_name: str | None = None
    patronymic: str | None = None
    country: str | None = None


class UserProfileUpdate(BaseModel):
    last_name: str | None = None
    first_name: str | None = None
    patronymic: str | None = None
    country: str | None = None
```

- [ ] **Step 2: Добавить репозиторные функции**

В конец `app/repositories/user.py` добавить (и в импортах модели вверху файла
добавить `UserProfile`: заменить
`from app.models.user import EmailVerificationToken, RefreshToken, User` на
`from app.models.user import EmailVerificationToken, RefreshToken, User, UserProfile`):

```python
async def get_profile(db: AsyncSession, user_id: UUID) -> UserProfile | None:
    return await db.get(UserProfile, user_id)


async def upsert_profile(
    db: AsyncSession, user_id: UUID, **fields
) -> UserProfile:
    """Создаёт или обновляет профиль. fields — только не-None значения."""
    profile = await db.get(UserProfile, user_id)
    if profile is None:
        profile = UserProfile(user_id=user_id, **fields)
        db.add(profile)
    else:
        for key, value in fields.items():
            setattr(profile, key, value)
    await db.flush()
    return profile
```

- [ ] **Step 3: Проверить импорт**

Run: `.\venv\Scripts\python.exe -c "from app.repositories.user import get_profile, upsert_profile; from app.schemas.user import UserProfileUpdate; print('ok')"`
Expected: печатает `ok`.

- [ ] **Step 4: Commit**

```bash
git add app/schemas/user.py app/repositories/user.py
git commit -m "feat(profile): schemas + repo for UserProfile"
```

---

### Task 8: Ручки профиля

**Files:**
- Modify: `app/routers/users.py`

- [ ] **Step 1: Добавить эндпоинты GET/PATCH профиля**

В `app/routers/users.py` в импортах из репозитория добавить `get_profile, upsert_profile`:
заменить
```python
from app.repositories.user import (
    get_user_by_id,
    revoke_all_refresh_tokens_for_user,
    update_user,
)
```
на
```python
from app.repositories.user import (
    get_profile,
    get_user_by_id,
    revoke_all_refresh_tokens_for_user,
    update_user,
    upsert_profile,
)
```
и в импорт схем добавить профиль:
```python
from app.schemas.user import (
    PublicUserResponse,
    UserProfileResponse,
    UserProfileUpdate,
    UserResponse,
    UserUpdate,
)
```

Перед `@router.get("/{user_id}", ...)` (публичный профиль) добавить:

```python
@router.get(
    "/me/profile",
    response_model=UserProfileResponse,
    summary="Мой профиль (ФИО/страна)",
)
async def get_my_profile(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    profile = await get_profile(db, current_user.id)
    if profile is None:
        # Профиль не заведён — отдаём пустой каркас, чтобы фронт показал
        # форму без 404.
        return UserProfileResponse()
    return UserProfileResponse.model_validate(profile)


@router.patch(
    "/me/profile",
    response_model=UserProfileResponse,
    summary="Обновить мой профиль (ФИО/страна)",
)
async def update_my_profile(
    payload: UserProfileUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    fields = payload.model_dump(exclude_unset=True)
    profile = await upsert_profile(db, current_user.id, **fields)
    await db.commit()
    return UserProfileResponse.model_validate(profile)
```

ВАЖНО: `/me/profile` должен идти **до** маршрута `/{user_id}`, иначе FastAPI
матчит `me` как `user_id`. Текущий `/{user_id}` объявлен последним —
разместить новые ручки выше него.

- [ ] **Step 2: Проверить, что приложение собирается**

Run: `.\venv\Scripts\python.exe -c "from app.routers import users; print([r.path for r in users.router.routes])"`
Expected: в списке присутствуют `/users/me/profile` (GET и PATCH) перед `/users/{user_id}`.

- [ ] **Step 3: Commit**

```bash
git add app/routers/users.py
git commit -m "feat(profile): GET/PATCH /users/me/profile"
```

---

## Фаза 2. Билдеры контекста документов

Архитектура каждого билдера: `build_*_context(db, ...)` загружает ORM-объекты
и зовёт **чистый** шейпер `_shape_*` (без БД) → словарь для docxtpl. Юнит-тесты
бьют по шейперам с фабричными объектами, без живой БД.

### Task 9: Контекст диплома (чистый шейпер + DB-билдер)

**Files:**
- Create: `app/services/document_official.py`
- Test: `tests/unit/test_official_context.py`

- [ ] **Step 1: Написать падающий тест на шейпер диплома**

```python
# tests/unit/test_official_context.py
import datetime as dt
from types import SimpleNamespace

from app.services.document_official import (
    _shape_diploma_context,
    DiplomaInput,
)


def test_shape_diploma_full():
    ctx = _shape_diploma_context(
        DiplomaInput(
            show_name="WORLD DOG SHOW 2025",
            judge="Никитина Ольга (Россия)",
            breed="Австралийская овчарка",
            sex="male",
            class_name="класс щенков",
            grade="отлично",
            title="CW, ЛПП",
            placement=1,
            dog_name="Bobby vom Haus",
            tattoo="ABC123",
            microchip="643094100123456",
            date_of_birth=dt.date(2024, 3, 1),
            owner="Петров Пётр",
            kennel="От Каховки",
            breeder="Сидорова Анна",
            pedigree="RKF1234567",
        )
    )
    assert ctx["show_name"] == "WORLD DOG SHOW 2025"
    assert ctx["judge"] == "Никитина Ольга (Россия)"
    assert ctx["sex_male"] is True
    assert ctx["sex_female"] is False
    assert ctx["dob"] == "01.03.2024"
    assert ctx["place"] == "1"
    assert ctx["dog_name"] == "Bobby vom Haus"
    assert ctx["pedigree"] == "RKF1234567"


def test_shape_diploma_empty_fields_become_blank_strings():
    ctx = _shape_diploma_context(
        DiplomaInput(
            show_name="X",
            judge=None,
            breed="Y",
            sex="female",
            class_name="откр.",
            grade=None,
            title=None,
            placement=None,
            dog_name="Z",
            tattoo=None,
            microchip=None,
            date_of_birth=None,
            owner=None,
            kennel=None,
            breeder=None,
            pedigree=None,
        )
    )
    assert ctx["sex_female"] is True
    assert ctx["sex_male"] is False
    assert ctx["grade"] == ""
    assert ctx["dob"] == ""
    assert ctx["place"] == ""
    assert ctx["judge"] == ""
```

- [ ] **Step 2: Запустить — упадёт (нет модуля)**

Run: `.\venv\Scripts\python.exe -m pytest tests/unit/test_official_context.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.document_official'`.

- [ ] **Step 3: Реализовать шейпер + входной dataclass + DB-билдер диплома**

```python
# app/services/document_official.py
"""
Билдеры контекста для официальных документов РКФ (docxtpl).

Разделение: чистые `_shape_*` собирают словарь из простых значений
(тестируются без БД); `build_*_context` грузят ORM и зовут шейпер.

Имена людей резолвятся через app.utils.names (profile должен быть
подгружен заранее).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dog import Dog
from app.models.kennel import Kennel
from app.models.reference import Breed, BreedGroup, Grade, ShowClass, ShowRank
from app.models.result import ShowResult
from app.models.show import Show, ShowEntry, ShowJudge, ShowRing
from app.models.user import User
from app.utils.names import full_name, judge_display


def _fmt_date(value: date | None) -> str:
    return value.strftime("%d.%m.%Y") if value else ""


def _s(value: object | None) -> str:
    return str(value) if value not in (None, "") else ""


# ---------------------------------------------------------------------
# Резолвер имён с профилем (async)
# ---------------------------------------------------------------------


async def _load_user_with_profile(
    db: AsyncSession, user_id: uuid.UUID | None
) -> User | None:
    if user_id is None:
        return None
    user = await db.get(User, user_id)
    if user is not None:
        # AsyncAttrs (Base) — ленивую связь profile грузим явно.
        await user.awaitable_attrs.profile
    return user


# ---------------------------------------------------------------------
# Диплом
# ---------------------------------------------------------------------


@dataclass
class DiplomaInput:
    show_name: str
    judge: str | None
    breed: str
    sex: str  # "male" | "female"
    class_name: str
    grade: str | None
    title: str | None
    placement: int | None
    dog_name: str
    tattoo: str | None
    microchip: str | None
    date_of_birth: date | None
    owner: str | None
    kennel: str | None
    breeder: str | None
    pedigree: str | None


def _shape_diploma_context(data: DiplomaInput) -> dict:
    return {
        "show_name": _s(data.show_name),
        "judge": _s(data.judge),
        "breed": _s(data.breed),
        "sex_male": data.sex == "male",
        "sex_female": data.sex == "female",
        "class_name": _s(data.class_name),
        "grade": _s(data.grade),
        "title": _s(data.title),
        "place": _s(data.placement),
        "dog_name": _s(data.dog_name),
        "tattoo": _s(data.tattoo),
        "microchip": _s(data.microchip),
        "dob": _fmt_date(data.date_of_birth),
        "owner": _s(data.owner),
        "kennel": _s(data.kennel),
        "breeder": _s(data.breeder),
        "pedigree": _s(data.pedigree),
    }


async def _resolve_breeder(
    db: AsyncSession, dog: Dog
) -> tuple[str, str]:
    """Возвращает (breeder_name, breeder_kennel_prefix)."""
    if dog.breeder_kennel_id is not None:
        kennel = await db.get(Kennel, dog.breeder_kennel_id)
        if kennel is not None:
            owner = await _load_user_with_profile(db, kennel.owner_id)
            return full_name(owner), _s(kennel.kennel_prefix or kennel.name)
    return _s(dog.breeder_name), ""


async def _resolve_owner(db: AsyncSession, dog: Dog) -> str:
    if dog.kennel_id is not None:
        kennel = await db.get(Kennel, dog.kennel_id)
        if kennel is not None:
            owner = await _load_user_with_profile(db, kennel.owner_id)
            return full_name(owner)
    return ""


async def build_diploma_context(
    db: AsyncSession, entry_id: uuid.UUID
) -> dict:
    entry = await db.get(ShowEntry, entry_id)
    if entry is None:
        raise ValueError("entry_not_found")
    show = await db.get(Show, entry.show_id)
    if show is None:
        raise ValueError("not_found")
    dog = await db.get(Dog, entry.dog_id)
    if dog is None:
        raise ValueError("dog_not_found")
    breed = await db.get(Breed, dog.breed_id)
    cls = await db.get(ShowClass, entry.show_class_id)

    result = (
        await db.execute(
            select(ShowResult).where(ShowResult.show_entry_id == entry_id)
        )
    ).scalar_one_or_none()

    grade_name = None
    judge = None
    title = None
    if result is not None:
        if result.grade_id is not None:
            grade = await db.get(Grade, result.grade_id)
            grade_name = grade.name if grade else None
        judge_user = await _load_user_with_profile(db, result.judge_id)
        judge = judge_display(judge_user) if judge_user else None
        titles = [
            t.get("name", t.get("code", ""))
            for t in (result.titles_cache or [])
        ]
        title = ", ".join(t for t in titles if t) or None

    owner = await _resolve_owner(db, dog)
    breeder, kennel_prefix = await _resolve_breeder(db, dog)

    return _shape_diploma_context(
        DiplomaInput(
            show_name=show.name,
            judge=judge,
            breed=breed.name if breed else "",
            sex=dog.sex.value,
            class_name=cls.name if cls else "",
            grade=grade_name,
            title=title,
            placement=result.placement if result else None,
            dog_name=dog.name,
            tattoo=dog.tattoo,
            microchip=dog.microchip,
            date_of_birth=dog.date_of_birth,
            owner=owner,
            kennel=kennel_prefix,
            breeder=breeder,
            pedigree=dog.rkf_number,
        )
    )
```

- [ ] **Step 4: Запустить — пройдёт**

Run: `.\venv\Scripts\python.exe -m pytest tests/unit/test_official_context.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add app/services/document_official.py tests/unit/test_official_context.py
git commit -m "feat(docs): diploma context builder + pure shaper"
```

---

### Task 10: Контекст ринговой ведомости

**Files:**
- Modify: `app/services/document_official.py`
- Modify: `tests/unit/test_official_context.py`

- [ ] **Step 1: Дописать падающий тест на шейпер ведомости**

В `tests/unit/test_official_context.py` добавить импорт и тест:

```python
from app.services.document_official import (  # noqa: E501  (дополнение импорта)
    _shape_ring_sheet,
    RingSheetInput,
    RingRowInput,
)


def test_shape_ring_sheet_rows_and_blank_columns():
    sheet = _shape_ring_sheet(
        RingSheetInput(
            city="г. Москва",
            date="13.07.2025",
            judge="Никитина Ольга (Россия)",
            breed="Австралийская овчарка",
            ring_number=1,
            class_name="класс щенков",
            sex="male",
            rows=[
                RingRowInput(
                    catalog_number=1,
                    dog_name="Bobby",
                    date_of_birth="01.03.2024",
                    color="блю-мерль",
                    pedigree="RKF1",
                    tattoo="T1",
                    microchip="C1",
                    breeder="Сидорова Анна",
                    owner="Петров Пётр",
                ),
            ],
        )
    )
    assert sheet["sex"] == "кобели"
    assert sheet["ring_number"] == "1"
    row = sheet["rows"][0]
    assert row["catalog_number"] == "1"
    assert "Bobby" in row["name_dob_color"]
    assert "01.03.2024" in row["name_dob_color"]
    assert "RKF1" in row["pedigree_marks"]
    assert "Сидорова Анна" in row["breeder_owner"]
    assert "Петров Пётр" in row["breeder_owner"]
```

- [ ] **Step 2: Запустить — упадёт (нет имён)**

Run: `.\venv\Scripts\python.exe -m pytest tests/unit/test_official_context.py -v`
Expected: FAIL — `ImportError: cannot import name '_shape_ring_sheet'`.

- [ ] **Step 3: Реализовать шейпер + билдер ведомости**

В `app/services/document_official.py` добавить:

```python
_SEX_RU = {"male": "кобели", "female": "суки"}


@dataclass
class RingRowInput:
    catalog_number: int | None
    dog_name: str
    date_of_birth: str  # уже форматированная дата
    color: str | None
    pedigree: str | None
    tattoo: str | None
    microchip: str | None
    breeder: str | None
    owner: str | None


@dataclass
class RingSheetInput:
    city: str | None
    date: str
    judge: str | None
    breed: str
    ring_number: int | None
    class_name: str
    sex: str
    rows: list[RingRowInput]


def _shape_ring_row(r: RingRowInput) -> dict:
    name_dob_color = ", ".join(
        x for x in [_s(r.dog_name), _s(r.date_of_birth), _s(r.color)] if x
    )
    marks = ", ".join(
        x for x in [_s(r.tattoo), _s(r.microchip)] if x
    )
    pedigree_marks = " / ".join(
        x for x in [_s(r.pedigree), marks] if x
    )
    breeder_owner = " / ".join(
        x for x in [_s(r.breeder), _s(r.owner)] if x
    )
    return {
        "catalog_number": _s(r.catalog_number),
        "name_dob_color": name_dob_color,
        "pedigree_marks": pedigree_marks,
        "breeder_owner": breeder_owner,
        # Пустые колонки — судья заполняет от руки.
        "grade": "",
        "titles": "",
        "place": "",
        "litter": "",
        "total": "",
    }


def _shape_ring_sheet(data: RingSheetInput) -> dict:
    return {
        "city": _s(data.city),
        "date": _s(data.date),
        "judge": _s(data.judge),
        "breed": _s(data.breed),
        "ring_number": _s(data.ring_number),
        "class_name": _s(data.class_name),
        "sex": _SEX_RU.get(data.sex, _s(data.sex)),
        "rows": [_shape_ring_row(r) for r in data.rows],
    }


async def build_ring_sheets_context(
    db: AsyncSession,
    show_id: uuid.UUID,
    ring_id: uuid.UUID | None = None,
) -> dict:
    """
    Контекст для одного файла со всеми ведомостями выставки (или одного
    ринга, если задан ring_id). Группировка: ринг → порода/класс → пол.

    Ведомость в образце сделана на (ринг + порода + класс + пол). Здесь
    собираем по рингам из ShowRing, а внутри ринга — по записям нужной
    породы/класса, разбивая по полу.
    """
    show = await db.get(Show, show_id)
    if show is None:
        raise ValueError("not_found")

    rings_stmt = select(ShowRing).where(ShowRing.show_id == show_id)
    if ring_id is not None:
        rings_stmt = rings_stmt.where(ShowRing.id == ring_id)
    rings_stmt = rings_stmt.order_by(ShowRing.ring_number.asc())
    rings = (await db.execute(rings_stmt)).scalars().all()

    sheets: list[dict] = []
    for ring in rings:
        if ring.breed_id is None:
            continue  # ведомость строится по конкретной породе ринга
        breed = await db.get(Breed, ring.breed_id)
        judge_user = await _load_user_with_profile(db, ring.judge_id)
        judge = judge_display(judge_user) if judge_user else None

        # Записи этой породы (через собак) в нужном классе ринга.
        entries = (
            await db.execute(
                select(ShowEntry)
                .where(ShowEntry.show_id == show_id)
                .order_by(ShowEntry.catalog_number.asc().nullslast())
            )
        ).scalars().all()

        cls = (
            await db.get(ShowClass, ring.show_class_id)
            if ring.show_class_id
            else None
        )

        # Разбиваем по полу.
        rows_by_sex: dict[str, list[RingRowInput]] = {"male": [], "female": []}
        for e in entries:
            dog = await db.get(Dog, e.dog_id)
            if dog is None or dog.breed_id != ring.breed_id:
                continue
            if ring.show_class_id and e.show_class_id != ring.show_class_id:
                continue
            breeder, _prefix = await _resolve_breeder(db, dog)
            owner = await _resolve_owner(db, dog)
            rows_by_sex[dog.sex.value].append(
                RingRowInput(
                    catalog_number=e.catalog_number,
                    dog_name=dog.name,
                    date_of_birth=_fmt_date(dog.date_of_birth),
                    color=dog.color,
                    pedigree=dog.rkf_number,
                    tattoo=dog.tattoo,
                    microchip=dog.microchip,
                    breeder=breeder,
                    owner=owner,
                )
            )

        ring_date = _fmt_date(ring.ring_date) or _fmt_date(show.date_start)
        for sex, rows in rows_by_sex.items():
            if not rows:
                continue
            sheets.append(
                _shape_ring_sheet(
                    RingSheetInput(
                        city=show.city,
                        date=ring_date,
                        judge=judge,
                        breed=breed.name if breed else "",
                        ring_number=ring.ring_number,
                        class_name=cls.name if cls else "",
                        sex=sex,
                        rows=rows,
                    )
                )
            )

    return {"sheets": sheets}
```

- [ ] **Step 4: Запустить — пройдёт**

Run: `.\venv\Scripts\python.exe -m pytest tests/unit/test_official_context.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add app/services/document_official.py tests/unit/test_official_context.py
git commit -m "feat(docs): ring sheet context builder + pure shaper"
```

---

### Task 11: Контекст каталога

**Files:**
- Modify: `app/services/document_official.py`
- Modify: `tests/unit/test_official_context.py`

- [ ] **Step 1: Дописать падающий тест на чистую группировку каталога**

В `tests/unit/test_official_context.py` добавить:

```python
from app.services.document_official import (  # дополнение импорта
    _shape_catalog,
    CatalogMeta,
    CatalogEntryInput,
)


def test_shape_catalog_groups_sorts_and_formats():
    meta = CatalogMeta(
        show_name="Выставка",
        show_rank="САС",
        period="13.07.2025",
        city="Москва",
        venue=None,
        judges=[{"name": "Судья А", "assignment": "группа FCI 1"}],
    )
    entries = [
        # порода группы 2 — должна идти после группы 1
        CatalogEntryInput(
            group_number=2, group_name="Пинчеры", breed_name="Доберман",
            fci_number="143", breed_judge="Судья Б",
            class_name="откр.", sex="male", catalog_number=10,
            dog_name="Rex", date_of_birth="01.01.2022", color="чёрный",
            pedigree="RKF10", tattoo="T", microchip="C",
            breeder="Зав1", owner="Вл1", sire="Отец", dam="Мать",
        ),
        CatalogEntryInput(
            group_number=1, group_name="Овчарки", breed_name="Аусси",
            fci_number="342", breed_judge="Судья А",
            class_name="щенков", sex="female", catalog_number=1,
            dog_name="Bella", date_of_birth="02.02.2024", color="мерль",
            pedigree="RKF1", tattoo=None, microchip=None,
            breeder="Зав2", owner="Вл2", sire=None, dam=None,
        ),
    ]
    ctx = _shape_catalog(meta, entries)
    assert ctx["show_name"] == "Выставка"
    assert [g["group_number"] for g in ctx["groups"]] == ["1", "2"]
    g1 = ctx["groups"][0]
    assert g1["breeds"][0]["breed_name"] == "Аусси"
    cls0 = g1["breeds"][0]["classes"][0]
    assert cls0["class_name"] == "щенков"
    assert cls0["entries"][0]["catalog_number"] == "1"
    assert cls0["entries"][0]["dog_name"] == "Bella"
```

- [ ] **Step 2: Запустить — упадёт**

Run: `.\venv\Scripts\python.exe -m pytest tests/unit/test_official_context.py -v`
Expected: FAIL — `ImportError: cannot import name '_shape_catalog'`.

- [ ] **Step 3: Реализовать шейпер + билдер каталога**

В `app/services/document_official.py` добавить:

```python
@dataclass
class CatalogMeta:
    show_name: str
    show_rank: str
    period: str
    city: str | None
    venue: str | None
    judges: list[dict]  # [{"name":..., "assignment":...}]


@dataclass
class CatalogEntryInput:
    group_number: int | None
    group_name: str | None
    breed_name: str
    fci_number: str | None
    breed_judge: str | None
    class_name: str
    sex: str  # "male"|"female"
    catalog_number: int | None
    dog_name: str
    date_of_birth: str
    color: str | None
    pedigree: str | None
    tattoo: str | None
    microchip: str | None
    breeder: str | None
    owner: str | None
    sire: str | None
    dam: str | None


def _shape_catalog_entry(e: CatalogEntryInput) -> dict:
    marks = " / ".join(x for x in [_s(e.tattoo), _s(e.microchip)] if x)
    return {
        "catalog_number": _s(e.catalog_number),
        "dog_name": _s(e.dog_name),
        "dob": _s(e.date_of_birth),
        "color": _s(e.color),
        "pedigree": _s(e.pedigree),
        "marks": marks,
        "breeder": _s(e.breeder),
        "owner": _s(e.owner),
        "sire": _s(e.sire),
        "dam": _s(e.dam),
    }


def _shape_catalog(meta: CatalogMeta, entries: list[CatalogEntryInput]) -> dict:
    """Группирует плоский список записей в группы FCI → породы → классы(+пол)."""
    # group_number None → в конец (999).
    def gkey(n: int | None) -> int:
        return n if n is not None else 999

    groups: dict[int, dict] = {}
    for e in entries:
        g = groups.setdefault(
            gkey(e.group_number),
            {
                "group_number": _s(e.group_number),
                "group_name": _s(e.group_name),
                "_breeds": {},
            },
        )
        b = g["_breeds"].setdefault(
            e.breed_name,
            {
                "breed_name": _s(e.breed_name),
                "fci_number": _s(e.fci_number),
                "judge": _s(e.breed_judge),
                "_classes": {},
            },
        )
        # ключ класса — (class_name, sex), пол важен для разбивки в каталоге
        ckey = (e.class_name, e.sex)
        c = b["_classes"].setdefault(
            ckey,
            {
                "class_name": _s(e.class_name),
                "sex": _SEX_RU.get(e.sex, _s(e.sex)),
                "entries": [],
            },
        )
        c["entries"].append(_shape_catalog_entry(e))

    # Разворачиваем словари в отсортированные списки.
    out_groups = []
    for gnum in sorted(groups.keys()):
        g = groups[gnum]
        breeds = []
        for bname in sorted(g["_breeds"].keys()):
            b = g["_breeds"][bname]
            classes = [b["_classes"][k] for k in b["_classes"]]
            breeds.append(
                {
                    "breed_name": b["breed_name"],
                    "fci_number": b["fci_number"],
                    "judge": b["judge"],
                    "classes": classes,
                }
            )
        out_groups.append(
            {
                "group_number": g["group_number"],
                "group_name": g["group_name"],
                "breeds": breeds,
            }
        )

    return {
        "show_name": _s(meta.show_name),
        "show_rank": _s(meta.show_rank),
        "period": _s(meta.period),
        "city": _s(meta.city),
        "venue": _s(meta.venue),
        "judges": meta.judges,
        "groups": out_groups,
        "total_entries": sum(len(e.dog_name) > 0 for e in entries) and len(entries),
    }


async def build_catalog_context(
    db: AsyncSession, show_id: uuid.UUID
) -> dict:
    show = await db.get(Show, show_id)
    if show is None:
        raise ValueError("not_found")
    rank = await db.get(ShowRank, show.rank_id)

    # Судьи в шапку.
    judges = (
        await db.execute(select(ShowJudge).where(ShowJudge.show_id == show_id))
    ).scalars().all()
    judges_meta: list[dict] = []
    judge_for_breed: dict[uuid.UUID, str] = {}
    for j in judges:
        assignment = "—"
        if j.breed_id is not None:
            br = await db.get(Breed, j.breed_id)
            if br is not None:
                assignment = f"порода: {br.name}"
            ju = await _load_user_with_profile(db, j.judge_id)
            if ju is not None:
                judge_for_breed[j.breed_id] = judge_display(ju)
        elif j.breed_group_id is not None:
            grp = await db.get(BreedGroup, j.breed_group_id)
            if grp is not None:
                assignment = f"группа FCI {grp.number}: {grp.name}"
        ju = await _load_user_with_profile(db, j.judge_id)
        judges_meta.append(
            {"name": judge_display(ju) if ju else "—", "assignment": assignment}
        )

    entries = (
        await db.execute(
            select(ShowEntry)
            .where(ShowEntry.show_id == show_id)
            .order_by(ShowEntry.catalog_number.asc().nullslast())
        )
    ).scalars().all()

    inputs: list[CatalogEntryInput] = []
    for e in entries:
        dog = await db.get(Dog, e.dog_id)
        if dog is None:
            continue
        breed = await db.get(Breed, dog.breed_id)
        group = (
            await db.get(BreedGroup, breed.breed_group_id)
            if breed and breed.breed_group_id
            else None
        )
        cls = await db.get(ShowClass, e.show_class_id)
        breeder, _prefix = await _resolve_breeder(db, dog)
        owner = await _resolve_owner(db, dog)
        sire = await db.get(Dog, dog.father_id) if dog.father_id else None
        dam = await db.get(Dog, dog.mother_id) if dog.mother_id else None
        inputs.append(
            CatalogEntryInput(
                group_number=group.number if group else None,
                group_name=group.name if group else None,
                breed_name=breed.name if breed else "",
                fci_number=breed.fci_number if breed else None,
                breed_judge=judge_for_breed.get(dog.breed_id),
                class_name=cls.name if cls else "",
                sex=dog.sex.value,
                catalog_number=e.catalog_number,
                dog_name=dog.name,
                date_of_birth=_fmt_date(dog.date_of_birth),
                color=dog.color,
                pedigree=dog.rkf_number,
                tattoo=dog.tattoo,
                microchip=dog.microchip,
                breeder=breeder,
                owner=owner,
                sire=sire.name if sire else None,
                dam=dam.name if dam else None,
            )
        )

    period = _fmt_date(show.date_start) + (
        f" — {_fmt_date(show.date_end)}" if show.date_end else ""
    )
    meta = CatalogMeta(
        show_name=show.name,
        show_rank=rank.name if rank else "",
        period=period,
        city=show.city,
        venue=show.venue,
        judges=judges_meta,
    )
    return _shape_catalog(meta, inputs)
```

ВНИМАНИЕ по `total_entries`: строка-выражение хитрая. Заменить её на явную:

```python
        "total_entries": len(entries),
```

— то есть в `_shape_catalog` параметр `entries` отражает все записи; используем
`len(entries)`. (Поправить в коде шейпера: `"total_entries": len(entries),`.)

- [ ] **Step 4: Запустить — пройдёт**

Run: `.\venv\Scripts\python.exe -m pytest tests/unit/test_official_context.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add app/services/document_official.py tests/unit/test_official_context.py
git commit -m "feat(docs): catalog context builder + grouping shaper"
```

---

### Task 12: Readiness (чек-лист пробелов)

**Files:**
- Modify: `app/services/document_official.py`
- Modify: `tests/unit/test_official_context.py`

- [ ] **Step 1: Дописать падающий тест на чистую проверку записи**

```python
from app.services.document_official import _entry_issues, EntryCheck


def test_entry_issues_flags_missing():
    issues = _entry_issues(
        EntryCheck(
            catalog_number=None, dog_name="Rex",
            owner_present=False, breeder_present=False,
            has_tattoo=False, has_microchip=False, has_pedigree=False,
        )
    )
    codes = {i["code"] for i in issues}
    assert "no_catalog_number" in codes
    assert "no_owner" in codes
    assert "no_breeder" in codes
    assert "no_id" in codes  # ни клейма, ни чипа
    assert "no_pedigree" in codes


def test_entry_issues_clean():
    issues = _entry_issues(
        EntryCheck(
            catalog_number=1, dog_name="Rex",
            owner_present=True, breeder_present=True,
            has_tattoo=True, has_microchip=False, has_pedigree=True,
        )
    )
    assert issues == []
```

- [ ] **Step 2: Запустить — упадёт**

Run: `.\venv\Scripts\python.exe -m pytest tests/unit/test_official_context.py -v`
Expected: FAIL — `ImportError: cannot import name '_entry_issues'`.

- [ ] **Step 3: Реализовать**

В `app/services/document_official.py` добавить:

```python
@dataclass
class EntryCheck:
    catalog_number: int | None
    dog_name: str
    owner_present: bool
    breeder_present: bool
    has_tattoo: bool
    has_microchip: bool
    has_pedigree: bool


def _entry_issues(c: EntryCheck) -> list[dict]:
    issues: list[dict] = []
    if c.catalog_number is None:
        issues.append({"code": "no_catalog_number", "message": "нет номера каталога"})
    if not c.owner_present:
        issues.append({"code": "no_owner", "message": "не указан владелец (ФИО)"})
    if not c.breeder_present:
        issues.append({"code": "no_breeder", "message": "не указан заводчик"})
    if not (c.has_tattoo or c.has_microchip):
        issues.append({"code": "no_id", "message": "нет клейма и чипа"})
    if not c.has_pedigree:
        issues.append({"code": "no_pedigree", "message": "нет № родословной"})
    return issues


async def build_documents_readiness(
    db: AsyncSession, show_id: uuid.UUID
) -> dict:
    """Список записей с проблемами, мешающими корректной печати документов."""
    show = await db.get(Show, show_id)
    if show is None:
        raise ValueError("not_found")
    entries = (
        await db.execute(select(ShowEntry).where(ShowEntry.show_id == show_id))
    ).scalars().all()

    problems: list[dict] = []
    for e in entries:
        dog = await db.get(Dog, e.dog_id)
        if dog is None:
            continue
        owner = await _resolve_owner(db, dog)
        breeder, _p = await _resolve_breeder(db, dog)
        check = EntryCheck(
            catalog_number=e.catalog_number,
            dog_name=dog.name,
            owner_present=bool(owner),
            breeder_present=bool(breeder),
            has_tattoo=bool(dog.tattoo),
            has_microchip=bool(dog.microchip),
            has_pedigree=bool(dog.rkf_number),
        )
        issues = _entry_issues(check)
        if issues:
            problems.append(
                {
                    "entry_id": str(e.id),
                    "dog_name": dog.name,
                    "catalog_number": e.catalog_number,
                    "issues": issues,
                }
            )
    return {"total_entries": len(entries), "problems": problems}
```

- [ ] **Step 4: Запустить — пройдёт**

Run: `.\venv\Scripts\python.exe -m pytest tests/unit/test_official_context.py -v`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add app/services/document_official.py tests/unit/test_official_context.py
git commit -m "feat(docs): documents readiness checklist builder"
```

---

## Фаза 3. DOCX-шаблоны

Шаблоны — бинарные .docx, созданные из RTF-образцов с сохранением оформления.
Их нельзя сгенерировать кодом 1-в-1, поэтому это ручная задача в Word/LibreOffice
с точным списком плейсхолдеров. Проверка — smoke-тест рендера.

### Task 13: Заготовки DOCX из RTF-образцов

**Files:**
- Create: `app/templates/documents/diploma.docx`
- Create: `app/templates/documents/ring_sheet.docx`
- Create: `app/templates/documents/catalog.docx`

- [ ] **Step 1: Сконвертировать образцы в DOCX**

Если установлен LibreOffice:
Run (PowerShell, по одному файлу):
```
soffice --headless --convert-to docx --outdir app/templates/documents "C:/Users/dmskd/Downloads/Telegram Desktop/Дипломы А4.rtf"
soffice --headless --convert-to docx --outdir app/templates/documents "C:/Users/dmskd/Downloads/Telegram Desktop/Ринговая ведомость.rtf"
soffice --headless --convert-to docx --outdir app/templates/documents "C:/Users/dmskd/Downloads/Telegram Desktop/САС 11 2025.rtf"
```
Затем переименовать получившиеся файлы в `diploma.docx`, `ring_sheet.docx`,
`catalog.docx`. Если LibreOffice нет — открыть каждый RTF в Word и
«Сохранить как» → .docx с этими именами.
Expected: три .docx в `app/templates/documents/`, открываются и визуально
совпадают с образцами.

- [ ] **Step 2: Commit заготовок (до разметки)**

```bash
git add app/templates/documents/diploma.docx app/templates/documents/ring_sheet.docx app/templates/documents/catalog.docx
git commit -m "chore(docs): DOCX заготовки шаблонов из RTF-образцов"
```

---

### Task 14: Разметка шаблона диплома

**Files:**
- Modify: `app/templates/documents/diploma.docx`
- Test: `tests/unit/test_official_templates.py`

- [ ] **Step 1: Написать smoke-тест рендера диплома**

```python
# tests/unit/test_official_templates.py
import pytest

from app.utils import docx_render


def _diploma_ctx():
    return {
        "show_name": "WORLD DOG SHOW 2025",
        "judge": "Никитина Ольга (Россия)",
        "breed": "Австралийская овчарка",
        "sex_male": True, "sex_female": False,
        "class_name": "класс щенков", "grade": "отлично",
        "title": "CW, ЛПП", "place": "1",
        "dog_name": "Bobby vom Haus", "tattoo": "ABC123",
        "microchip": "643094100123456", "dob": "01.03.2024",
        "owner": "Петров Пётр", "kennel": "От Каховки",
        "breeder": "Сидорова Анна", "pedigree": "RKF1234567",
    }


def test_diploma_template_renders_with_substitutions():
    body = docx_render.render_docx("diploma.docx", _diploma_ctx())
    assert body[:2] == b"PK"  # zip-сигнатура docx
    assert len(body) > 2000
```

- [ ] **Step 2: Запустить — упадёт (нет плейсхолдеров → docxtpl отрендерит, но проверим, что не падает)**

Run: `.\venv\Scripts\python.exe -m pytest tests/unit/test_official_templates.py::test_diploma_template_renders_with_substitutions -v`
Expected: на этом этапе тест может пройти (пустой шаблон рендерится), но
визуально подстановок нет. Это нормально — разметку добавляем в Step 3,
тест защищает от поломки рендера.

- [ ] **Step 3: Вставить плейсхолдеры в diploma.docx**

Открыть `app/templates/documents/diploma.docx` в Word/LibreOffice. На месте
точек-пропусков («………») вписать ровно эти теги (Jinja-синтаксис docxtpl).
Каждый тег — обычный текст в нужной графе:

| Графа бланка | Тег |
|---|---|
| Выставка / DOG SHOW | `{{ show_name }}` |
| Эксперт / JUDGE (и подпись) | `{{ judge }}` |
| Порода / BREED | `{{ breed }}` |
| Класс / CLASS | `{{ class_name }}` |
| Оценка / GRADE | `{{ grade }}` |
| Титул / TITLE | `{{ title }}` |
| Место / PLACE | `{{ place }}` |
| Кличка / NAME | `{{ dog_name }}` |
| № клейма / TATTOO | `{{ tattoo }}` |
| № чипа / MICROCHIP | `{{ microchip }}` |
| Дата рождения | `{{ dob }}` |
| Владелец / OWNER | `{{ owner }}` |
| Питомник / KENNEL | `{{ kennel }}` |
| Заводчик / BREEDER | `{{ breeder }}` |
| Родословная № / PEDIGREE | `{{ pedigree }}` |

Для отметки пола (КОБЕЛИ/СУКИ) рядом с каждой подписью поставить условный
маркер: возле «КОБЕЛИ/MALES» — `{% if sex_male %}X{% endif %}`, возле
«СУКИ/FEMALES» — `{% if sex_female %}X{% endif %}`.
Сохранить файл (формат .docx).

ВАЖНО: вписывая тег, следить, чтобы он не разбивался Word'ом на несколько
«runs» (из-за автозамены/проверки орфографии). Надёжно: набрать тег в одном
стиле/одним заходом, не редактируя по символу. Если docxtpl ругается на
сломанный тег — выделить тег и переписать заново.

- [ ] **Step 4: Запустить smoke-тест — должен пройти**

Run: `.\venv\Scripts\python.exe -m pytest tests/unit/test_official_templates.py::test_diploma_template_renders_with_substitutions -v`
Expected: PASS. Дополнительно вручную: открыть результат
(`render_docx` → сохранить в файл) и убедиться, что значения подставились.

- [ ] **Step 5: Commit**

```bash
git add app/templates/documents/diploma.docx tests/unit/test_official_templates.py
git commit -m "feat(docs): размечен шаблон диплома (docxtpl плейсхолдеры)"
```

---

### Task 15: Разметка шаблона ринговой ведомости

**Files:**
- Modify: `app/templates/documents/ring_sheet.docx`
- Modify: `tests/unit/test_official_templates.py`

- [ ] **Step 1: Дописать smoke-тест ведомости**

```python
def _ring_ctx():
    return {
        "sheets": [
            {
                "city": "г. Москва", "date": "13.07.2025",
                "judge": "Никитина Ольга (Россия)",
                "breed": "Австралийская овчарка", "ring_number": "1",
                "class_name": "класс щенков", "sex": "кобели",
                "rows": [
                    {
                        "catalog_number": "1",
                        "name_dob_color": "Bobby, 01.03.2024, блю-мерль",
                        "pedigree_marks": "RKF1 / T1 / C1",
                        "breeder_owner": "Сидорова Анна / Петров Пётр",
                        "grade": "", "titles": "", "place": "",
                        "litter": "", "total": "",
                    }
                ],
            }
        ]
    }


def test_ring_sheet_template_renders():
    body = docx_render.render_docx("ring_sheet.docx", _ring_ctx())
    assert body[:2] == b"PK"
    assert len(body) > 2000
```

- [ ] **Step 2: Запустить (red/safety)**

Run: `.\venv\Scripts\python.exe -m pytest tests/unit/test_official_templates.py::test_ring_sheet_template_renders -v`
Expected: пройдёт после разметки в Step 3; до неё проверяет, что рендер не падает.

- [ ] **Step 3: Разметить ring_sheet.docx**

Открыть `ring_sheet.docx`. Шапку заполнить тегами: город `{{ sheets ... }}` —
нет: шапка повторяется для каждого листа, поэтому **вся ведомость должна быть
внутри цикла по `sheets`**.

В DOCX это делается так: весь блок одного листа (шапка + таблица) обернуть
парой абзацев-тегов:
- перед блоком листа — абзац с `{% for sheet in sheets %}`
- после блока (после таблицы) — абзац с `{% endfor %}`
- чтобы каждый лист печатался с новой страницы — внутри цикла после таблицы
  вставить разрыв страницы.

В шапке листа вписать: `{{ sheet.city }}`, `{{ sheet.date }}`,
`{{ sheet.judge }}`, `{{ sheet.breed }}`, `{{ sheet.ring_number }}`,
`{{ sheet.class_name }}`, `{{ sheet.sex }}`.

В таблице участников строку данных (одну) сделать повторяемой через
`{%tr ... %}` теги docxtpl в ПЕРВОЙ и последней ячейке строки:
- в первой ячейке строки-образца в начале текста: `{%tr for row in sheet.rows %}`
- в последней ячейке этой же строки в конце: `{%tr endfor %}`

Ячейки строки заполнить:
| Колонка | Тег |
|---|---|
| № кат. | `{{ row.catalog_number }}` |
| Клички, дата рожд., окрас | `{{ row.name_dob_color }}` |
| № родословной, клеймо/чип | `{{ row.pedigree_marks }}` |
| Заводчик, владелец | `{{ row.breeder_owner }}` |
| Оценка | `{{ row.grade }}` (пусто) |
| Звания, титулы | `{{ row.titles }}` (пусто) |
| Место | `{{ row.place }}` (пусто) |
| Выводок | `{{ row.litter }}` (пусто) |
| Сумма | `{{ row.total }}` (пусто) |

Сохранить .docx.

- [ ] **Step 4: Запустить smoke-тест**

Run: `.\venv\Scripts\python.exe -m pytest tests/unit/test_official_templates.py::test_ring_sheet_template_renders -v`
Expected: PASS. Вручную открыть результат и проверить, что таблица
размножилась по строкам, а пустые колонки пустые.

- [ ] **Step 5: Commit**

```bash
git add app/templates/documents/ring_sheet.docx tests/unit/test_official_templates.py
git commit -m "feat(docs): размечен шаблон ринговой ведомости (циклы по листам/строкам)"
```

---

### Task 16: Разметка шаблона каталога

**Files:**
- Modify: `app/templates/documents/catalog.docx`
- Modify: `tests/unit/test_official_templates.py`

- [ ] **Step 1: Дописать smoke-тест каталога**

```python
def _catalog_ctx():
    return {
        "show_name": "Региональная выставка ранга САС",
        "show_rank": "САС", "period": "13.07.2025",
        "city": "Москва", "venue": "Крокус", "total_entries": 1,
        "judges": [{"name": "Судья А (Россия)", "assignment": "группа FCI 1"}],
        "groups": [
            {
                "group_number": "1", "group_name": "Овчарки",
                "breeds": [
                    {
                        "breed_name": "Австралийская овчарка",
                        "fci_number": "342", "judge": "Судья А (Россия)",
                        "classes": [
                            {
                                "class_name": "класс щенков", "sex": "суки",
                                "entries": [
                                    {
                                        "catalog_number": "1",
                                        "dog_name": "Bella",
                                        "dob": "02.02.2024", "color": "мерль",
                                        "pedigree": "RKF1", "marks": "T / C",
                                        "breeder": "Зав", "owner": "Вл",
                                        "sire": "Отец", "dam": "Мать",
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
    }


def test_catalog_template_renders():
    body = docx_render.render_docx("catalog.docx", _catalog_ctx())
    assert body[:2] == b"PK"
    assert len(body) > 2000
```

- [ ] **Step 2: Запустить (safety)**

Run: `.\venv\Scripts\python.exe -m pytest tests/unit/test_official_templates.py::test_catalog_template_renders -v`
Expected: пройдёт после разметки; до неё — проверка, что рендер не падает.

- [ ] **Step 3: Разметить catalog.docx**

Открыть `catalog.docx`. Шапка: `{{ show_name }}`, `{{ show_rank }}`,
`{{ period }}`, `{{ city }}`, `{{ venue }}`, `{{ total_entries }}`.
Список судей — повторяемый абзац: `{%p for j in judges %}` … `{{ j.name }} — {{ j.assignment }}` … `{%p endfor %}` (теги `{%p ...%}` — для повтора абзаца целиком).

Тело каталога — вложенные циклы (абзацные теги `{%p %}`):
- группа: `{%p for group in groups %}` → заголовок `Группа FCI {{ group.group_number }}. {{ group.group_name }}`
  - порода: `{%p for breed in group.breeds %}` → `{{ breed.breed_name }} (FCI {{ breed.fci_number }}). Судья: {{ breed.judge }}`
    - класс: `{%p for cls in breed.classes %}` → `{{ cls.class_name }} — {{ cls.sex }}`
      - записи — в таблице со строкой-циклом: в первой ячейке строки-образца
        `{%tr for e in cls.entries %}`, в последней `{%tr endfor %}`; ячейки:
        `{{ e.catalog_number }}`, `{{ e.dog_name }}`, `{{ e.dob }}`,
        `{{ e.color }}`, `{{ e.pedigree }}`, `{{ e.marks }}`,
        `{{ e.breeder }}`, `{{ e.owner }}`, `{{ e.sire }}`, `{{ e.dam }}`
      - `{%p endfor %}` (класс)
    - `{%p endfor %}` (порода)
- `{%p endfor %}` (группа)

Если в образце каталога записи идут не таблицей, а абзацами — тогда вместо
`{%tr%}` использовать `{%p for e in cls.entries %}` … `{%p endfor %}` и
вписать поля в один абзац в порядке образца. Выбрать вариант по факту
оформления образца. Сохранить .docx.

- [ ] **Step 4: Запустить smoke-тест**

Run: `.\venv\Scripts\python.exe -m pytest tests/unit/test_official_templates.py::test_catalog_template_renders -v`
Expected: PASS. Вручную открыть результат, проверить группировку/порядок.

- [ ] **Step 5: Commit**

```bash
git add app/templates/documents/catalog.docx tests/unit/test_official_templates.py
git commit -m "feat(docs): размечен шаблон каталога (вложенные циклы)"
```

---

## Фаза 4. Воркер и API

### Task 17: Новые типы задач + ветки воркера

**Files:**
- Modify: `app/schemas/task.py`
- Modify: `worker/handlers/document_handler.py`

- [ ] **Step 1: Добавить значения DocumentKind**

В `app/schemas/task.py` в `class DocumentKind` после `DIPLOMAS_BATCH` добавить:

```python
    CATALOG_OFFICIAL = "generate_catalog_official"
    DIPLOMA_OFFICIAL = "generate_diploma_official"
    DIPLOMAS_BATCH_OFFICIAL = "generate_diplomas_batch_official"
    RING_SHEETS_OFFICIAL = "generate_ring_sheets_official"
```

- [ ] **Step 2: Расширить `_upload_and_register` параметром extension**

В `worker/handlers/document_handler.py` в сигнатуре `_upload_and_register`
заменить хардкод расширения. Найти:

```python
async def _upload_and_register(
    db: AsyncSession,
    body: bytes,
    filename: str,
    *,
    content_type: str,
    created_by: uuid.UUID | None,
) -> uuid.UUID:
```
заменить на:
```python
async def _upload_and_register(
    db: AsyncSession,
    body: bytes,
    filename: str,
    *,
    content_type: str,
    created_by: uuid.UUID | None,
    extension: str = "pdf",
) -> uuid.UUID:
```
и внутри тела найти вызов:
```python
    s3_key, size_bytes = await file_storage.upload_bytes(
        body,
        content_type=content_type,
        extension="pdf",
        folder="documents",
    )
```
заменить `extension="pdf"` на `extension=extension`.

- [ ] **Step 3: Добавить официальные хендлеры + диспетч**

В `worker/handlers/document_handler.py` добавить импорты вверху:

```python
import asyncio

from app.services import document_official
from app.utils import docx_render
```

Добавить функции (после `_handle_diplomas_batch`):

```python
async def _render_official(
    template_name: str, context: dict, fmt: str, basename: str
) -> tuple[bytes, str, str, str]:
    """Рендерит docx и (опц.) конвертит в pdf. Блокирующие вызовы — в
    отдельном потоке, чтобы не вешать event loop воркера.
    Возвращает (body, extension, content_type, filename)."""
    docx_bytes = await asyncio.to_thread(
        docx_render.render_docx, template_name, context
    )
    if fmt == "pdf":
        pdf_bytes = await asyncio.to_thread(
            docx_render.convert_docx_to_pdf, docx_bytes
        )
        return pdf_bytes, "pdf", "application/pdf", f"{basename}.pdf"
    return (
        docx_bytes,
        "docx",
        docx_render.DOCX_CONTENT_TYPE,
        f"{basename}.docx",
    )


async def _handle_catalog_official(db, payload, created_by):
    show_id = uuid.UUID(payload["show_id"])
    fmt = payload.get("format", "docx")
    ctx = await document_official.build_catalog_context(db, show_id)
    body, ext, ctype, filename = await _render_official(
        "catalog.docx", ctx, fmt, f"catalog_official_{show_id}"
    )
    return await _upload_and_register(
        db, body, filename, content_type=ctype, created_by=created_by, extension=ext
    )


async def _handle_diploma_official(db, payload, created_by):
    entry_id = uuid.UUID(payload["entry_id"])
    fmt = payload.get("format", "docx")
    ctx = await document_official.build_diploma_context(db, entry_id)
    body, ext, ctype, filename = await _render_official(
        "diploma.docx", ctx, fmt, f"diploma_official_{entry_id}"
    )
    return await _upload_and_register(
        db, body, filename, content_type=ctype, created_by=created_by, extension=ext
    )


async def _handle_ring_sheets_official(db, payload, created_by):
    show_id = uuid.UUID(payload["show_id"])
    fmt = payload.get("format", "docx")
    ring_id = payload.get("ring_id")
    ring_uuid = uuid.UUID(ring_id) if ring_id else None
    ctx = await document_official.build_ring_sheets_context(db, show_id, ring_uuid)
    body, ext, ctype, filename = await _render_official(
        "ring_sheet.docx", ctx, fmt, f"ring_sheets_{show_id}"
    )
    return await _upload_and_register(
        db, body, filename, content_type=ctype, created_by=created_by, extension=ext
    )
```

В функции `process_document_task` в цепочке `if/elif task.type == ...`
ДОБАВИТЬ ветки (перед `else: raise ValueError`):

```python
        elif task.type == DocumentKind.CATALOG_OFFICIAL.value:
            file_id = await _handle_catalog_official(db, task.payload, task.created_by)
        elif task.type == DocumentKind.DIPLOMA_OFFICIAL.value:
            file_id = await _handle_diploma_official(db, task.payload, task.created_by)
        elif task.type == DocumentKind.RING_SHEETS_OFFICIAL.value:
            file_id = await _handle_ring_sheets_official(db, task.payload, task.created_by)
```

(Пакет официальных дипломов `DIPLOMAS_BATCH_OFFICIAL` — отдельная ветка в
Task 18; здесь оставляем 3 ветки.)

- [ ] **Step 4: Проверить, что воркер импортируется**

Run: `.\venv\Scripts\python.exe -c "import worker.handlers.document_handler as h; print(hasattr(h, '_handle_catalog_official'))"`
Expected: печатает `True`.

- [ ] **Step 5: Commit**

```bash
git add app/schemas/task.py worker/handlers/document_handler.py
git commit -m "feat(worker): official document task kinds + docx/pdf handlers"
```

---

### Task 18: Пакет официальных дипломов (один файл со всеми)

**Files:**
- Modify: `app/services/document_official.py`
- Modify: `worker/handlers/document_handler.py`

- [ ] **Step 1: Билдер контекста пакета дипломов**

В `app/services/document_official.py` добавить:

```python
async def build_diplomas_batch_context(
    db: AsyncSession, show_id: uuid.UUID
) -> dict:
    """Контекст для одного файла со всеми дипломами выставки."""
    entry_ids = (
        await db.execute(
            select(ShowEntry.id).where(ShowEntry.show_id == show_id)
        )
    ).scalars().all()
    diplomas = []
    for eid in entry_ids:
        try:
            diplomas.append(await build_diploma_context(db, eid))
        except ValueError:
            continue
    return {"diplomas": diplomas}
```

Для пакета нужен отдельный шаблон-обёртка `diplomas_batch.docx`: один
диплом в цикле `{% for d in diplomas %}` … `{% endfor %}` с разрывом
страницы между ними, поля — `{{ d.show_name }}` и т.д. (та же разметка,
что в diploma.docx, но обёрнута в цикл и поля идут через `d.`).
СОЗДАНИЕ ШАБЛОНА: скопировать `diploma.docx` → `diplomas_batch.docx`,
обернуть содержимое в `{% for d in diplomas %}`/`{% endfor %}`, заменить
`{{ field }}` на `{{ d.field }}`, добавить разрыв страницы в конце цикла.

- [ ] **Step 2: Хендлер пакета**

В `worker/handlers/document_handler.py` добавить:

```python
async def _handle_diplomas_batch_official(db, payload, created_by):
    show_id = uuid.UUID(payload["show_id"])
    fmt = payload.get("format", "docx")
    ctx = await document_official.build_diplomas_batch_context(db, show_id)
    body, ext, ctype, filename = await _render_official(
        "diplomas_batch.docx", ctx, fmt, f"diplomas_official_{show_id}"
    )
    return await _upload_and_register(
        db, body, filename, content_type=ctype, created_by=created_by, extension=ext
    )
```

В `process_document_task` добавить ветку:

```python
        elif task.type == DocumentKind.DIPLOMAS_BATCH_OFFICIAL.value:
            file_id = await _handle_diplomas_batch_official(db, task.payload, task.created_by)
```

- [ ] **Step 3: Создать diplomas_batch.docx и smoke-тест**

В `tests/unit/test_official_templates.py` добавить:

```python
def test_diplomas_batch_template_renders():
    ctx = {"diplomas": [_diploma_ctx(), _diploma_ctx()]}
    body = docx_render.render_docx("diplomas_batch.docx", ctx)
    assert body[:2] == b"PK"
```

Создать шаблон как описано в Step 1.

- [ ] **Step 4: Запустить тест**

Run: `.\venv\Scripts\python.exe -m pytest tests/unit/test_official_templates.py::test_diplomas_batch_template_renders -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/document_official.py worker/handlers/document_handler.py app/templates/documents/diplomas_batch.docx tests/unit/test_official_templates.py
git commit -m "feat(docs): batch official diplomas (one file)"
```

---

### Task 19: Официальные эндпоинты генерации

**Files:**
- Modify: `app/routers/documents.py`

- [ ] **Step 1: Расширить DocumentKind-импорт и добавить эндпоинты**

В `app/routers/documents.py` убедиться, что `DocumentKind` импортирован
(он уже импортируется из `app.schemas.task`). В конец файла добавить:

```python
# ---------------------------------------------------------------------
# Официальные документы (формат РКФ) — отдельные ручки рядом со старыми.
# Формат вывода — query-параметр format=docx|pdf, кладётся в payload задачи.
# ---------------------------------------------------------------------


def _norm_format(fmt: str) -> str:
    if fmt not in ("docx", "pdf"):
        raise HTTPException(400, "format must be docx or pdf")
    return fmt


@router.post(
    "/{show_id}/official/catalog",
    response_model=TaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Каталог выставки в формате РКФ (docx/pdf)",
)
async def generate_official_catalog(
    show_id: uuid.UUID,
    format: str = "docx",
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    fmt = _norm_format(format)
    try:
        await _ensure_organizer(db, show_id, user)
    except ValueError as e:
        _raise_for_error(e)
    return await _publish_task(
        db, user, DocumentKind.CATALOG_OFFICIAL,
        {"show_id": str(show_id), "format": fmt},
    )


@router.post(
    "/{show_id}/official/diplomas",
    response_model=TaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Пакет дипломов в формате РКФ (docx/pdf)",
)
async def generate_official_diplomas(
    show_id: uuid.UUID,
    format: str = "docx",
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    fmt = _norm_format(format)
    try:
        await _ensure_organizer(db, show_id, user)
    except ValueError as e:
        _raise_for_error(e)
    return await _publish_task(
        db, user, DocumentKind.DIPLOMAS_BATCH_OFFICIAL,
        {"show_id": str(show_id), "format": fmt},
    )


@router.post(
    "/{show_id}/entries/{entry_id}/official/diploma",
    response_model=TaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Диплом участника в формате РКФ (docx/pdf)",
)
async def generate_official_diploma(
    show_id: uuid.UUID,
    entry_id: uuid.UUID,
    format: str = "docx",
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    fmt = _norm_format(format)
    try:
        await _ensure_organizer(db, show_id, user)
    except ValueError as e:
        _raise_for_error(e)
    return await _publish_task(
        db, user, DocumentKind.DIPLOMA_OFFICIAL,
        {"show_id": str(show_id), "entry_id": str(entry_id), "format": fmt},
    )


@router.post(
    "/{show_id}/official/ring-sheets",
    response_model=TaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ринговые ведомости в формате РКФ (docx/pdf)",
)
async def generate_official_ring_sheets(
    show_id: uuid.UUID,
    format: str = "docx",
    ring_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    fmt = _norm_format(format)
    try:
        await _ensure_organizer(db, show_id, user)
    except ValueError as e:
        _raise_for_error(e)
    payload = {"show_id": str(show_id), "format": fmt}
    if ring_id is not None:
        payload["ring_id"] = str(ring_id)
    return await _publish_task(
        db, user, DocumentKind.RING_SHEETS_OFFICIAL, payload
    )
```

- [ ] **Step 2: Проверить маршруты**

Run: `.\venv\Scripts\python.exe -c "from app.routers import documents as d; print([r.path for r in d.router.routes if 'official' in r.path])"`
Expected: 4 пути с `official`.

- [ ] **Step 3: Commit**

```bash
git add app/routers/documents.py
git commit -m "feat(api): official document generation endpoints (docx/pdf)"
```

---

### Task 20: Ручки context-предпросмотр и readiness

**Files:**
- Modify: `app/routers/documents.py`

- [ ] **Step 1: Добавить импорт билдеров и сериализатора**

В `app/routers/documents.py` в импортах добавить:

```python
from app.services import document_official
from app.services.document import to_jsonable
```

- [ ] **Step 2: Добавить эндпоинты**

В конец `app/routers/documents.py` добавить:

```python
@router.get(
    "/{show_id}/official/{kind}/context",
    summary="Данные документа для предпросмотра/правки на фронте",
)
async def get_official_context(
    show_id: uuid.UUID,
    kind: str,
    entry_id: uuid.UUID | None = None,
    ring_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        await _ensure_organizer(db, show_id, user)
    except ValueError as e:
        _raise_for_error(e)
    try:
        if kind == "catalog":
            ctx = await document_official.build_catalog_context(db, show_id)
        elif kind == "ring-sheets":
            ctx = await document_official.build_ring_sheets_context(
                db, show_id, ring_id
            )
        elif kind == "diploma":
            if entry_id is None:
                raise HTTPException(400, "entry_id required for diploma")
            ctx = await document_official.build_diploma_context(db, entry_id)
        else:
            raise HTTPException(404, "unknown document kind")
    except ValueError as e:
        _raise_for_error(e)
    return to_jsonable(ctx)


@router.get(
    "/{show_id}/documents/readiness",
    summary="Чек-лист пробелов перед печатью документов",
)
async def get_documents_readiness(
    show_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        await _ensure_organizer(db, show_id, user)
        data = await document_official.build_documents_readiness(db, show_id)
    except ValueError as e:
        _raise_for_error(e)
    return data
```

- [ ] **Step 3: Проверить маршруты**

Run: `.\venv\Scripts\python.exe -c "from app.routers import documents as d; print([r.path for r in d.router.routes if 'context' in r.path or 'readiness' in r.path])"`
Expected: два пути (`/shows/{show_id}/official/{kind}/context`, `/shows/{show_id}/documents/readiness`).

- [ ] **Step 4: Commit**

```bash
git add app/routers/documents.py
git commit -m "feat(api): official context preview + documents readiness endpoints"
```

---

## Фаза 5. Финал

### Task 21: Прогон всех тестов и очистка временных файлов

**Files:**
- Delete: `_tmp_rtf/`, `_tmp_rtf_extract.py`, `_tmp_rtf_utf8.py`

- [ ] **Step 1: Прогнать весь тест-сьют**

Run: `.\venv\Scripts\python.exe -m pytest -q`
Expected: все тесты зелёные (новые + старые). Если падают тесты, требующие
БД/Rabbit — они и раньше не входили в локальный прогон; смотреть только на
unit-тесты этой фичи (`tests/unit/test_names.py`,
`tests/unit/test_official_context.py`, `tests/unit/test_docx_render.py`,
`tests/unit/test_official_templates.py`).

- [ ] **Step 2: Удалить временные артефакты парсинга**

Run (PowerShell):
```
Remove-Item -Recurse -Force _tmp_rtf -ErrorAction SilentlyContinue
Remove-Item -Force _tmp_rtf_extract.py, _tmp_rtf_utf8.py -ErrorAction SilentlyContinue
```
Expected: файлы/папка удалены (их не было в git — это очистка рабочей копии).

- [ ] **Step 3: Проверить, что .docx-шаблоны не игнорируются git**

Run: `git status --porcelain app/templates/documents`
Expected: либо пусто (уже закоммичены), либо видны .docx как tracked. Если
`.gitignore` исключает `*.docx` — добавить исключение
`!app/templates/documents/*.docx`.

- [ ] **Step 4: Commit (если есть изменения)**

```bash
git add -A
git commit -m "chore: cleanup temp RTF parsing artifacts"
```

---

### Task 22: Документация знаний

**Files:**
- Create: `docs/knowledge/official-documents-docx.md`

- [ ] **Step 1: Записать заметку в базу знаний**

Создать `docs/knowledge/official-documents-docx.md` с разделами: зачем
docxtpl (а не ReportLab) для официальных бланков; как устроены шаблоны
(`{{ }}`, `{%tr%}`, `{%p%}`, `{% for %}`); поток API→воркер→docx→(LibreOffice)→PDF;
как добавить новый документ (шаблон + билдер контекста + ветка воркера +
ручка); требование LibreOffice для PDF; разделение заводчик/владелец на `Dog`.

- [ ] **Step 2: Commit**

```bash
git add docs/knowledge/official-documents-docx.md
git commit -m "docs(knowledge): official RKF documents via docxtpl"
```

---

## Self-Review (выполнено при написании)

- **Покрытие спеки:** §1 поля → Tasks 9–11 (контексты) + 14–16 (шаблоны);
  §2.1 профиль → Tasks 3,5,7,8 + хелперы Task 6; §2.2 заводчик → Tasks 4,5,9;
  §3 архитектура → Tasks 2,9–12,17–18; §4 API → Tasks 8,19,20; §5 тесты →
  юнит-тесты в каждой задаче; §6 риски (LibreOffice) → Task 2 +
  knowledge Task 22; очистка → Task 21.
- **Плейсхолдеры:** код приведён полностью; ручная разметка .docx неизбежна
  (бинарный формат) — для неё даны точные теги и smoke-тесты.
- **Согласованность типов:** `_render_official` возвращает
  `(body, ext, content_type, filename)` и так же используется во всех
  хендлерах; `_upload_and_register(..., extension=...)` — единая сигнатура;
  `DocumentKind.*_OFFICIAL` значения совпадают между schemas и ветками воркера.
- **Известное упрощение:** `total_entries` в `_shape_catalog` — использовать
  `len(entries)` (см. примечание в Task 11 Step 3).

## Вне объёма

- Параметризация брендинга (логотипы/«WORLD DOG SHOW 2025») — литералы шаблона.
- Автозаполнение заводчика из `Litter` — после связи `Dog.litter_id`.
- Сертификаты CAC/CACIB — отдельная итерация.
- Установка LibreOffice в Docker — этап 15 (Dockerfile воркера).
