# Show Check-in Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Регистрация прибытия участников на выставку: документы собаки, персонал выставки, QR-билет, стойка регистратора с отметками прибытия/ветконтроля/документов, автоматическая неявка, блокировка результатов недопущенных и напоминания о документах.

**Architecture:** Бэкенд (FastAPI) — новые таблицы `dog_documents`, `show_staff`, `entry_checks` и колонки `shows.checkin_enabled`, `show_entries.attendance_status`; чистые правила в `checkin_rules.py`, HMAC-токен в `utils/checkin_token.py`, сервис/роутер чек-ина и документов. Фронтенд (Next.js + MUI + SWR) — вкладка документов собаки, страница «Мой билет», страницы организатора (персонал, предпроверка, стойка) и список выставок регистратора.

**Tech Stack:** FastAPI, SQLAlchemy 2.0 async, Alembic, PostgreSQL, Redis, APScheduler, pytest(-asyncio); Next.js 15, MUI 7, SWR, vitest, `qrcode.react`, `qr-scanner`.

**Spec:** `docs/superpowers/specs/2026-10-04-show-checkin-design.md`

## Global Constraints

- Бэкенд-ветка `feature/show-checkin` (от `origin/main`) в `show-ring-backend`; фронтенд-ветка `feature/show-checkin` (от `origin/main`) в `show-ring-frontend`.
- Токен: формат `SR1.<base64url(show_id 16 байт + user_id 16 байт)>.<base64url(HMAC-SHA256[:16])>`, без padding, сравнение через `hmac.compare_digest`.
- Ключ токена — `settings.checkin_token_secret`; если пуст — производный ключ `HMAC(secret_key, "show-ring/checkin-token/v1")` (отклонение от спеки: не валим прод-старт без новой переменной; ключ всё равно отделён от JWT-подписи).
- Окна статусов: `docs_precheck` — `registration_open`/`registration_closed`; `arrival`/`vet`/`docs_onsite`, scan, search — `registration_closed`/`in_progress`; билет — `registration_open`/`registration_closed`/`in_progress`.
- Вся логика неявки/блокировки результатов/напоминаний — только при `shows.checkin_enabled = true`.
- `failed`-отметка без комментария → 422.
- Файлы документов — `UploadedFile.is_public = False`.
- Комментарии и тексты UI — на русском; идентификаторы — английские; стиль кода — как в соседних файлах (докстринги с «зачем», `ValueError("code")` в сервисах → маппинг в роутере).
- Напоминания доставляются адресно (in_app `Notification` + WS push + transactional email), а не через `publish_event`: события в проекте рассылаются по подпискам и не умеют адресовать конкретного пользователя (отклонение от спеки). Дедупликация — детерминированный `message_id = uuid5(...)` + UNIQUE в `notifications` (надёжнее Redis-ключа).

## Review Focus

1. Повторный скан того же QR после отметок — карточка показывает актуальные `latest_checks` и статус, а не дублирует записи. (Test: Task 6, `test_scan_after_checks_shows_latest_state`.)
2. Исправление ошибки регистратора: «Не допустить» → затем «Допустить» → статус `admitted`, обе отметки в истории. (Test: Task 3 unit + Task 6 `test_correction_flips_rejected_to_admitted`.)
3. Опоздавший после старта выставки: `absent` → отметка `arrival` → `arrived`. (Test: Task 7 `test_late_arrival_after_absent`.)
4. Регистратор другой выставки пытается сканировать/отмечать/скачивать документ — 403/404, а не утечка ПДн. (Test: Task 6 `test_foreign_registrar_forbidden`, Task 4 `test_download_acl`.)
5. Выставка без чек-ина (флаг выключен) при старте не получает массовую неявку и результаты вносятся как раньше. (Test: Task 7 `test_no_absent_when_checkin_disabled`.)

---

## Окружение для запуска тестов (бэкенд)

Проектный `.venv` собран на другой машине — используется scratch-venv. Postgres/Redis — изолированные контейнеры `showring-test-pg` (55432) и `showring-test-redis` (56379), как в CI.

```bash
source "$SCRATCH/testenv.sh"   # экспортирует DATABASE_URL, REDIS_URL, SECRET_KEY, PY
cd show-ring-backend
"$PY" -m alembic upgrade head
"$PY" -m pytest tests/unit/test_checkin_token.py -q
```

---

### Task 1: Модели, миграция, настройка ключа

**Files:**
- Modify: `app/models/dog.py` (добавить `DogDocumentKind`, `DogDocument`)
- Modify: `app/models/show.py` (`Show.checkin_enabled`, `AttendanceStatus`, `ShowEntry.attendance_status/attendance_changed_at`, `ShowStaffRole`, `ShowStaff`, `EntryCheckKind`, `EntryCheckResult`, `EntryCheck`)
- Modify: `app/config.py` (`checkin_token_secret: str = ""`)
- Create: `migrations/versions/f4a5b6c7d8e9_show_checkin.py`
- Test: `tests/integration/test_checkin_models.py`

**Interfaces:**
- Produces: `DogDocumentKind{vet_passport,pedigree,puppy_card,working_certificate,other}`, `DogDocument(id, dog_id, file_id, kind, valid_until, uploaded_by, created_at)`; `AttendanceStatus{registered,arrived,admitted,rejected,absent}`; `ShowStaffRole{registrar}`; `ShowStaff(id, show_id, user_id, role, added_by, created_at)`; `EntryCheckKind{docs_precheck,arrival,vet,docs_onsite}`; `EntryCheckResult{passed,failed}`; `EntryCheck(id, entry_id, kind, result, document_id, comment, performed_by, created_at)`; `Show.checkin_enabled: bool`; `ShowEntry.attendance_status: AttendanceStatus`, `ShowEntry.attendance_changed_at: datetime | None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_checkin_models.py
"""Интеграция: новые таблицы чек-ина создаются миграцией и пишутся ORM."""

from __future__ import annotations

import uuid
from datetime import date

from app.models.dog import DogDocument, DogDocumentKind
from app.models.file import UploadedFile
from app.models.show import (
    AttendanceStatus,
    EntryCheck,
    EntryCheckKind,
    EntryCheckResult,
    ShowStaff,
    ShowStaffRole,
)
from tests.integration.checkin_helpers import make_world


async def test_checkin_tables_roundtrip(db_session):
    w = await make_world(db_session)
    assert w.show.checkin_enabled is True
    assert w.entry.attendance_status == AttendanceStatus.registered

    f = UploadedFile(
        uploaded_by=w.owner.id, s3_key=f"dog-documents/{uuid.uuid4()}.pdf",
        original_filename="vet.pdf", content_type="application/pdf",
        size_bytes=10, is_public=False,
    )
    db_session.add(f)
    await db_session.flush()
    doc = DogDocument(
        dog_id=w.dog.id, file_id=f.id, kind=DogDocumentKind.vet_passport,
        valid_until=date(2030, 1, 1), uploaded_by=w.owner.id,
    )
    staff = ShowStaff(
        show_id=w.show.id, user_id=w.owner.id, role=ShowStaffRole.registrar,
        added_by=w.organizer.id,
    )
    db_session.add_all([doc, staff])
    await db_session.flush()
    check = EntryCheck(
        entry_id=w.entry.id, kind=EntryCheckKind.vet,
        result=EntryCheckResult.passed, document_id=doc.id,
        performed_by=w.organizer.id,
    )
    db_session.add(check)
    await db_session.commit()
    assert check.created_at is not None
    assert doc.created_at is not None
```

```python
# tests/integration/checkin_helpers.py
"""
Общие фикстуры-хелперы для тестов чек-ина. Не тест-модуль (нет test_
префикса) — импортируется из test_*.py.

Справочники (тип животного, порода, ранг, класс) создаются здесь же,
а не берутся из сидов: CI-база содержит только миграции, и тесты на
сидах там молча скипаются.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, timedelta

from app.models.dog import Dog, SexEnum
from app.models.reference import AnimalType, Breed, ShowClass, ShowRank
from app.models.show import Show, ShowEntry, ShowStatus
from app.models.user import User

PASSWORD = "secret123"


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def make_api_user(client) -> tuple[uuid.UUID, str]:
    """Регистрирует и логинит пользователя через API: (id, access_token)."""
    email = f"chk_{uuid.uuid4().hex[:10]}@example.com"
    await client.post("/auth/register", json={"email": email, "password": PASSWORD})
    r = await client.post(
        "/auth/login",
        json={"email": email, "password": PASSWORD},
        headers={"X-Token-Delivery": "body"},
    )
    access = r.json()["access_token"]
    me = await client.get("/users/me", headers=auth(access))
    return uuid.UUID(me.json()["id"]), access


async def make_db_user(db_session, *, phone: str | None = None) -> User:
    u = User(
        email=None if phone else f"u_{uuid.uuid4().hex[:8]}@example.com",
        phone=phone,
        hashed_password="x",
    )
    db_session.add(u)
    await db_session.flush()
    return u


@dataclass
class World:
    organizer: User
    owner: User
    show: Show
    dog: Dog
    entry: ShowEntry
    show_class: ShowClass
    breed: Breed


async def make_references(db_session, *, class_code: str = "open"):
    suffix = uuid.uuid4().hex[:6]
    at = AnimalType(code=f"dog_{suffix}", name="Собаки")
    db_session.add(at)
    await db_session.flush()
    breed = Breed(animal_type_id=at.id, code=f"breed_{suffix}", name="Лабрадор")
    rank = ShowRank(code=f"rank_{suffix}", name="КЧК")
    cls = ShowClass(
        animal_type_id=at.id, code=class_code, name="Открытый",
        age_from_months=15, age_to_months=None,
    )
    db_session.add_all([breed, rank, cls])
    await db_session.flush()
    return breed, rank, cls


async def make_world(
    db_session,
    *,
    status: ShowStatus = ShowStatus.registration_closed,
    checkin_enabled: bool = True,
    date_start: date | None = None,
    class_code: str = "open",
    organizer: User | None = None,
    owner: User | None = None,
) -> World:
    breed, rank, cls = await make_references(db_session, class_code=class_code)
    organizer = organizer or await make_db_user(db_session)
    owner = owner or await make_db_user(db_session)
    show = Show(
        organizer_id=organizer.id, rank_id=rank.id, name="Чек-ин выставка",
        date_start=date_start or date.today(), status=status,
        checkin_enabled=checkin_enabled,
    )
    dog = Dog(
        breed_id=breed.id, name=f"Рекс {uuid.uuid4().hex[:4]}",
        sex=SexEnum.male, date_of_birth=date.today() - timedelta(days=900),
        owner_id=owner.id, microchip=f"6430{uuid.uuid4().int % 10**11:011d}",
    )
    db_session.add_all([show, dog])
    await db_session.flush()
    entry = ShowEntry(
        show_id=show.id, dog_id=dog.id, show_class_id=cls.id,
        registered_by=owner.id, catalog_number=1,
    )
    db_session.add(entry)
    await db_session.commit()
    return World(organizer, owner, show, dog, entry, cls, breed)


async def add_entry(db_session, world: World, *, owner: User, name: str = "Белка") -> ShowEntry:
    dog = Dog(
        breed_id=world.breed.id, name=name, sex=SexEnum.female,
        date_of_birth=date.today() - timedelta(days=800), owner_id=owner.id,
    )
    db_session.add(dog)
    await db_session.flush()
    entry = ShowEntry(
        show_id=world.show.id, dog_id=dog.id, show_class_id=world.show_class.id,
        registered_by=owner.id,
    )
    db_session.add(entry)
    await db_session.commit()
    return entry
```

- [ ] **Step 2: Run test to verify it fails**

Run: `"$PY" -m pytest tests/integration/test_checkin_models.py -q`
Expected: FAIL — `ImportError: cannot import name 'DogDocument'`.

- [ ] **Step 3: Implement models**

`app/models/dog.py` — добавить в импорты `DateTime`, `func` из sqlalchemy и `datetime` из datetime; в конец файла:

```python
class DogDocumentKind(str, enum.Enum):
    vet_passport = "vet_passport"                 # ветпаспорт (прививка от бешенства)
    pedigree = "pedigree"                         # родословная
    puppy_card = "puppy_card"                     # щенячья карточка / метрика
    working_certificate = "working_certificate"   # рабочий сертификат (рабочий класс)
    other = "other"


class DogDocument(Base):
    """
    Документ собаки (скан) для допуска на выставки.

    Документы принадлежат СОБАКЕ и переиспользуются между выставками;
    решение о допуске — отметки на конкретной записи (entry_checks).
    Одного вида может быть несколько (новый ветпаспорт после
    ревакцинации) — действующий = последний по created_at.

    valid_until — для vet_passport срок действия прививки от бешенства
    (вводит владелец при загрузке); для остальных видов обычно NULL.
    Файл (files.is_public=False) — ПДн и ветданные, публично не отдаётся.
    """

    __tablename__ = "dog_documents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    dog_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dogs.id", ondelete="CASCADE"), index=True
    )
    file_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("files.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[DogDocumentKind] = mapped_column(
        SAEnum(DogDocumentKind, name="dogdocumentkind")
    )
    valid_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
```

`app/models/show.py` — в импорты добавить `Boolean`, `DateTime`, `func`, `datetime`; в `Show` после `status`:

```python
    # Регистрация прибытия (чек-ин). Выключено по умолчанию: выставки,
    # которые не пользуются стойкой, не должны получать массовую «неявку»
    # при старте и блокировку результатов.
    checkin_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
```

Перед `class ShowEntry` — enum, в `ShowEntry` после `notes`:

```python
class AttendanceStatus(str, enum.Enum):
    registered = "registered"  # записана, на стойке ещё не отмечена
    arrived = "arrived"        # прибыла, проверки не завершены
    admitted = "admitted"      # прибыла + ветконтроль + документы пройдены
    rejected = "rejected"      # не допущена (ветконтроль или документы)
    absent = "absent"          # не явилась к старту выставки
```

```python
    # Статус явки/допуска. Вручную не ставится — пересчитывается сервисом
    # чек-ина из журнала entry_checks (checkin_rules.compute_attendance_status).
    attendance_status: Mapped[AttendanceStatus] = mapped_column(
        SAEnum(AttendanceStatus, name="attendancestatus"),
        default=AttendanceStatus.registered,
        server_default=AttendanceStatus.registered.value,
        index=True,
    )
    attendance_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
```

В конец файла:

```python
class ShowStaffRole(str, enum.Enum):
    registrar = "registrar"


class ShowStaff(Base):
    """
    Персонал конкретной выставки (регистраторы стойки).

    Роль в рамках ОДНОЙ выставки, а не глобальная RoleEnum: волонтёру
    стойки не нужны права организатора на всей платформе.
    """

    __tablename__ = "show_staff"
    __table_args__ = (
        UniqueConstraint("show_id", "user_id", "role", name="uq_show_staff"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    show_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("shows.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[ShowStaffRole] = mapped_column(
        SAEnum(ShowStaffRole, name="showstaffrole"), default=ShowStaffRole.registrar
    )
    added_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class EntryCheckKind(str, enum.Enum):
    docs_precheck = "docs_precheck"  # организатор до выставки, по сканам
    arrival = "arrival"              # стойка: собака прибыла
    vet = "vet"                      # стойка: ветконтроль
    docs_onsite = "docs_onsite"      # стойка: сверка документов/чипа


class EntryCheckResult(str, enum.Enum):
    passed = "passed"
    failed = "failed"


class EntryCheck(Base):
    """
    Журнал отметок по записи (append-only).

    Строки не обновляются: ошибка регистратора исправляется новой
    отметкой того же вида, действующая — последняя по created_at.
    Так история «кто и когда что отметил» и есть аудит.
    """

    __tablename__ = "entry_checks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    entry_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("show_entries.id", ondelete="CASCADE"),
        index=True,
    )
    kind: Mapped[EntryCheckKind] = mapped_column(
        SAEnum(EntryCheckKind, name="entrycheckkind")
    )
    result: Mapped[EntryCheckResult] = mapped_column(
        SAEnum(EntryCheckResult, name="entrycheckresult")
    )
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dog_documents.id", ondelete="SET NULL"),
        nullable=True,
    )
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    performed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
```

`app/config.py` — рядом с `frontend_base_url`:

```python
    # Ключ HMAC-подписи QR-билетов чек-ина (app/utils/checkin_token.py).
    # Пусто → производный ключ из SECRET_KEY с доменным разделением:
    # прод не падает на старте без новой переменной, а подпись билетов
    # всё равно не совпадает с подписью JWT.
    checkin_token_secret: str = ""
```

- [ ] **Step 4: Write migration**

```python
# migrations/versions/f4a5b6c7d8e9_show_checkin.py
"""show_checkin

Revision ID: f4a5b6c7d8e9
Revises: e3c4d5e6f7a8
Create Date: 2026-10-04 12:00:00.000000

Регистрация прибытия (чек-ин): документы собак, персонал выставки,
журнал отметок, флаг выставки и статус явки записи.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "f4a5b6c7d8e9"
down_revision: Union[str, Sequence[str], None] = "e3c4d5e6f7a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ATTENDANCE = postgresql.ENUM(
    "registered", "arrived", "admitted", "rejected", "absent",
    name="attendancestatus",
)


def upgrade() -> None:
    op.add_column(
        "shows",
        sa.Column("checkin_enabled", sa.Boolean(), nullable=False, server_default="false"),
    )

    _ATTENDANCE.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "show_entries",
        sa.Column(
            "attendance_status",
            postgresql.ENUM(name="attendancestatus", create_type=False),
            nullable=False,
            server_default="registered",
        ),
    )
    op.add_column(
        "show_entries",
        sa.Column("attendance_changed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_show_entries_attendance_status", "show_entries", ["attendance_status"]
    )

    op.create_table(
        "dog_documents",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("dog_id", sa.UUID(), sa.ForeignKey("dogs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("file_id", sa.UUID(), sa.ForeignKey("files.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "vet_passport", "pedigree", "puppy_card", "working_certificate", "other",
                name="dogdocumentkind",
            ),
            nullable=False,
        ),
        sa.Column("valid_until", sa.Date(), nullable=True),
        sa.Column("uploaded_by", sa.UUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_dog_documents_dog_id", "dog_documents", ["dog_id"])
    op.create_index("ix_dog_documents_file_id", "dog_documents", ["file_id"])

    op.create_table(
        "show_staff",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("show_id", sa.UUID(), sa.ForeignKey("shows.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.Enum("registrar", name="showstaffrole"), nullable=False),
        sa.Column("added_by", sa.UUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("show_id", "user_id", "role", name="uq_show_staff"),
    )
    op.create_index("ix_show_staff_show_id", "show_staff", ["show_id"])
    op.create_index("ix_show_staff_user_id", "show_staff", ["user_id"])

    op.create_table(
        "entry_checks",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("entry_id", sa.UUID(), sa.ForeignKey("show_entries.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "kind",
            sa.Enum("docs_precheck", "arrival", "vet", "docs_onsite", name="entrycheckkind"),
            nullable=False,
        ),
        sa.Column("result", sa.Enum("passed", "failed", name="entrycheckresult"), nullable=False),
        sa.Column("document_id", sa.UUID(), sa.ForeignKey("dog_documents.id", ondelete="SET NULL"), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("performed_by", sa.UUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_entry_checks_entry_id", "entry_checks", ["entry_id"])


def downgrade() -> None:
    op.drop_index("ix_entry_checks_entry_id", table_name="entry_checks")
    op.drop_table("entry_checks")
    op.drop_index("ix_show_staff_user_id", table_name="show_staff")
    op.drop_index("ix_show_staff_show_id", table_name="show_staff")
    op.drop_table("show_staff")
    op.drop_index("ix_dog_documents_file_id", table_name="dog_documents")
    op.drop_index("ix_dog_documents_dog_id", table_name="dog_documents")
    op.drop_table("dog_documents")
    op.drop_index("ix_show_entries_attendance_status", table_name="show_entries")
    op.drop_column("show_entries", "attendance_changed_at")
    op.drop_column("show_entries", "attendance_status")
    op.drop_column("shows", "checkin_enabled")
    for enum_name in (
        "entrycheckresult", "entrycheckkind", "showstaffrole",
        "dogdocumentkind", "attendancestatus",
    ):
        sa.Enum(name=enum_name).drop(op.get_bind(), checkfirst=True)
```

- [ ] **Step 5: Apply migration and run test**

Run: `"$PY" -m alembic upgrade head && "$PY" -m alembic downgrade -1 && "$PY" -m alembic upgrade head && "$PY" -m pytest tests/integration/test_checkin_models.py -q`
Expected: миграция туда-обратно без ошибок; `1 passed`.

- [ ] **Step 6: Commit**

```bash
git add app/models/dog.py app/models/show.py app/config.py migrations/versions/f4a5b6c7d8e9_show_checkin.py tests/integration/checkin_helpers.py tests/integration/test_checkin_models.py
git commit -m "feat(checkin): модели и миграция чек-ина"
```

---

### Task 2: HMAC-токен QR-билета

**Files:**
- Create: `app/utils/checkin_token.py`
- Test: `tests/unit/test_checkin_token.py`

**Interfaces:**
- Produces: `make_token(show_id: UUID, user_id: UUID, *, key: bytes | None = None) -> str`; `parse_token(token: str, *, key: bytes | None = None) -> tuple[UUID, UUID]`; `class InvalidCheckinToken(ValueError)` с `.reason in {"malformed","unsupported_version","bad_signature"}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_checkin_token.py
import uuid

import pytest

from app.utils.checkin_token import InvalidCheckinToken, make_token, parse_token

KEY = b"k" * 32
S, U = uuid.uuid4(), uuid.uuid4()


def test_roundtrip():
    token = make_token(S, U, key=KEY)
    assert token.startswith("SR1.")
    assert len(token) < 80
    assert parse_token(token, key=KEY) == (S, U)


def test_every_char_flip_is_rejected():
    token = make_token(S, U, key=KEY)
    for i, ch in enumerate(token):
        if ch == ".":
            continue
        repl = "A" if ch != "A" else "B"
        tampered = token[:i] + repl + token[i + 1:]
        with pytest.raises(InvalidCheckinToken):
            parse_token(tampered, key=KEY)


def test_wrong_key_bad_signature():
    token = make_token(S, U, key=KEY)
    with pytest.raises(InvalidCheckinToken) as e:
        parse_token(token, key=b"x" * 32)
    assert e.value.reason == "bad_signature"


def test_other_version_unsupported():
    token = make_token(S, U, key=KEY).replace("SR1.", "SR2.", 1)
    with pytest.raises(InvalidCheckinToken) as e:
        parse_token(token, key=KEY)
    assert e.value.reason == "unsupported_version"


@pytest.mark.parametrize(
    "garbage", ["", "SR1", "SR1..", "SR1.a.b.c", "hello world", "SR1.!!!.???", "x" * 500]
)
def test_garbage_malformed(garbage):
    with pytest.raises(InvalidCheckinToken) as e:
        parse_token(garbage, key=KEY)
    assert e.value.reason == "malformed"


def test_default_key_from_settings():
    token = make_token(S, U)
    assert parse_token(token) == (S, U)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `"$PY" -m pytest tests/unit/test_checkin_token.py -q`
Expected: FAIL — `ModuleNotFoundError: app.utils.checkin_token`.

- [ ] **Step 3: Implement**

```python
# app/utils/checkin_token.py
"""
Токен QR-билета чек-ина.

Формат: SR1.<base64url(show_id 16 байт + user_id 16 байт)>.<base64url(HMAC[:16])>

Принцип: токен ИДЕНТИФИЦИРУЕТ участника, но НЕ АВТОРИЗУЕТ действий.
Скан лишь ускоряет поиск на стойке; права даёт роль регистратора, а
допуск — проверка собаки на месте. Поэтому без срока действия и без
хранения в БД: подпись пересчитывается при каждом скане.

Почему не JWT: QR из ~70 символов — версия 4–5, читается бюджетной
камерой; JWT в 3–4 раза длиннее, а его claims здесь не нужны.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import uuid

from app.config import settings

VERSION = "SR1"
_SIG_LEN = 16
_MAX_LEN = 200
_VERSION_RE = re.compile(r"SR\d+")


class InvalidCheckinToken(ValueError):
    """reason: malformed | unsupported_version | bad_signature."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _secret() -> bytes:
    if settings.checkin_token_secret:
        return settings.checkin_token_secret.encode()
    # Доменное разделение: производный ключ ≠ ключ подписи JWT.
    return hmac.new(
        settings.secret_key.encode(), b"show-ring/checkin-token/v1", hashlib.sha256
    ).digest()


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    # Каноничность: urlsafe_b64decode молча игнорирует мусор и «лишние»
    # младшие биты последнего символа — без этой проверки два разных
    # токена давали бы одинаковые байты.
    if _b64e(raw) != text:
        raise ValueError("non-canonical base64")
    return raw


def _sign(signed_part: str, key: bytes) -> bytes:
    return hmac.new(key, signed_part.encode("ascii"), hashlib.sha256).digest()[:_SIG_LEN]


def make_token(
    show_id: uuid.UUID, user_id: uuid.UUID, *, key: bytes | None = None
) -> str:
    signed_part = f"{VERSION}.{_b64e(show_id.bytes + user_id.bytes)}"
    return f"{signed_part}.{_b64e(_sign(signed_part, key or _secret()))}"


def parse_token(
    token: str, *, key: bytes | None = None
) -> tuple[uuid.UUID, uuid.UUID]:
    if not isinstance(token, str) or not token or len(token) > _MAX_LEN:
        raise InvalidCheckinToken("malformed")
    parts = token.strip().split(".")
    if len(parts) != 3:
        raise InvalidCheckinToken("malformed")
    version, payload, sig = parts
    if version != VERSION:
        if _VERSION_RE.fullmatch(version):
            raise InvalidCheckinToken("unsupported_version")
        raise InvalidCheckinToken("malformed")
    try:
        raw = _b64d(payload)
        sig_raw = _b64d(sig)
    except (binascii.Error, ValueError):
        raise InvalidCheckinToken("malformed") from None
    if len(raw) != 32 or len(sig_raw) != _SIG_LEN:
        raise InvalidCheckinToken("malformed")
    expected = _sign(f"{version}.{payload}", key or _secret())
    if not hmac.compare_digest(expected, sig_raw):
        raise InvalidCheckinToken("bad_signature")
    return uuid.UUID(bytes=raw[:16]), uuid.UUID(bytes=raw[16:])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `"$PY" -m pytest tests/unit/test_checkin_token.py -q`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add app/utils/checkin_token.py tests/unit/test_checkin_token.py
git commit -m "feat(checkin): подписанный HMAC-токен QR-билета"
```

---

### Task 3: Доменные правила чек-ина

**Files:**
- Create: `app/services/checkin_rules.py`
- Test: `tests/unit/test_checkin_rules.py`

**Interfaces:**
- Consumes: enums из Task 1; `show_rules.WORKING_CLASS_CODE`.
- Produces: `PRECHECK_STATUSES`, `ONSITE_STATUSES`, `TICKET_STATUSES` (frozenset[ShowStatus]); `reference_date(date_start, date_end) -> date`; `current_documents(docs) -> dict[DogDocumentKind, D]`; `rabies_valid_for(current, ref_date) -> bool | None`; `document_problems(current, ref_date, class_code) -> list[str]`; `PROBLEM_LABELS: dict[str, str]`; `compute_attendance_status(latest: Mapping[EntryCheckKind, EntryCheckResult], current: AttendanceStatus) -> AttendanceStatus`; `allowed_statuses_for(kind: EntryCheckKind) -> frozenset[ShowStatus]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_checkin_rules.py
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import pytest

from app.models.dog import DogDocumentKind as K
from app.models.show import AttendanceStatus as A
from app.models.show import EntryCheckKind as C
from app.models.show import EntryCheckResult as R
from app.services import checkin_rules as rules


@dataclass
class Doc:
    kind: K
    created_at: datetime
    valid_until: date | None = None


T0 = datetime(2026, 1, 1)
REF = date(2026, 10, 10)


def test_reference_date_prefers_end():
    assert rules.reference_date(date(2026, 1, 1), date(2026, 1, 2)) == date(2026, 1, 2)
    assert rules.reference_date(date(2026, 1, 1), None) == date(2026, 1, 1)


def test_current_documents_latest_wins():
    old = Doc(K.vet_passport, T0, date(2026, 1, 1))
    new = Doc(K.vet_passport, T0 + timedelta(days=1), date(2027, 1, 1))
    cur = rules.current_documents([new, old])
    assert cur[K.vet_passport] is new


def test_problems_all_missing():
    assert rules.document_problems({}, REF, "open") == [
        rules.VET_PASSPORT_MISSING, rules.PEDIGREE_MISSING,
    ]


def test_problems_expired_on_reference_date():
    cur = rules.current_documents([
        Doc(K.vet_passport, T0, REF - timedelta(days=1)),
        Doc(K.puppy_card, T0),
    ])
    assert rules.document_problems(cur, REF, "open") == [rules.RABIES_EXPIRED]


def test_problems_valid_until_equal_reference_ok_and_date_missing():
    ok = rules.current_documents([Doc(K.vet_passport, T0, REF), Doc(K.pedigree, T0)])
    assert rules.document_problems(ok, REF, "open") == []
    no_date = rules.current_documents([Doc(K.vet_passport, T0), Doc(K.pedigree, T0)])
    assert rules.document_problems(no_date, REF, "open") == [rules.RABIES_DATE_MISSING]


def test_working_class_requires_certificate():
    cur = rules.current_documents([Doc(K.vet_passport, T0, REF), Doc(K.pedigree, T0)])
    assert rules.document_problems(cur, REF, "working") == [rules.WORKING_CERTIFICATE_MISSING]
    cur[K.working_certificate] = Doc(K.working_certificate, T0)
    assert rules.document_problems(cur, REF, "working") == []


def test_rabies_valid_for():
    assert rules.rabies_valid_for({}, REF) is None
    cur = rules.current_documents([Doc(K.vet_passport, T0, REF)])
    assert rules.rabies_valid_for(cur, REF) is True
    assert rules.rabies_valid_for(cur, REF + timedelta(days=1)) is False


@pytest.mark.parametrize(
    ("latest", "current", "expected"),
    [
        ({}, A.registered, A.registered),
        ({}, A.absent, A.absent),
        ({C.arrival: R.passed}, A.registered, A.arrived),
        ({C.arrival: R.passed}, A.absent, A.arrived),
        ({C.arrival: R.passed, C.vet: R.passed}, A.registered, A.arrived),
        ({C.arrival: R.passed, C.vet: R.passed, C.docs_onsite: R.passed}, A.arrived, A.admitted),
        ({C.arrival: R.passed, C.vet: R.failed}, A.arrived, A.rejected),
        ({C.arrival: R.passed, C.vet: R.passed, C.docs_onsite: R.failed}, A.admitted, A.rejected),
        ({C.vet: R.failed}, A.registered, A.rejected),
        ({C.arrival: R.failed}, A.arrived, A.registered),
        ({C.docs_precheck: R.passed}, A.registered, A.registered),
    ],
)
def test_compute_attendance_status(latest, current, expected):
    assert rules.compute_attendance_status(latest, current) == expected


def test_allowed_statuses_for():
    from app.models.show import ShowStatus as S
    assert rules.allowed_statuses_for(C.docs_precheck) == {S.registration_open, S.registration_closed}
    assert rules.allowed_statuses_for(C.vet) == {S.registration_closed, S.in_progress}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `"$PY" -m pytest tests/unit/test_checkin_rules.py -q`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Implement**

```python
# app/services/checkin_rules.py
"""
Доменные правила чек-ина (чистые функции, без БД).

Отдельно от сервиса по той же причине, что и show_rules.py: правила
допуска меняются, их удобно читать и тестировать в одном месте.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import Protocol, TypeVar

from app.models.dog import DogDocumentKind
from app.models.show import (
    AttendanceStatus,
    EntryCheckKind,
    EntryCheckResult,
    ShowStatus,
)
from app.services.show_rules import WORKING_CLASS_CODE

PRECHECK_STATUSES = frozenset({ShowStatus.registration_open, ShowStatus.registration_closed})
ONSITE_STATUSES = frozenset({ShowStatus.registration_closed, ShowStatus.in_progress})
TICKET_STATUSES = frozenset(
    {ShowStatus.registration_open, ShowStatus.registration_closed, ShowStatus.in_progress}
)

VET_PASSPORT_MISSING = "vet_passport_missing"
RABIES_DATE_MISSING = "rabies_date_missing"
RABIES_EXPIRED = "rabies_expired"
PEDIGREE_MISSING = "pedigree_missing"
WORKING_CERTIFICATE_MISSING = "working_certificate_missing"

PROBLEM_LABELS: dict[str, str] = {
    VET_PASSPORT_MISSING: "Не загружен ветпаспорт",
    RABIES_DATE_MISSING: "Не указан срок действия прививки от бешенства",
    RABIES_EXPIRED: "Прививка от бешенства истекает до окончания выставки",
    PEDIGREE_MISSING: "Не загружена родословная или метрика",
    WORKING_CERTIFICATE_MISSING: "Для рабочего класса нужен рабочий сертификат",
}


class _Doc(Protocol):
    kind: DogDocumentKind
    valid_until: date | None
    created_at: datetime


D = TypeVar("D", bound=_Doc)


def reference_date(date_start: date, date_end: date | None) -> date:
    """Прививка должна действовать весь период выставки — сверяем по последнему дню."""
    return date_end or date_start


def allowed_statuses_for(kind: EntryCheckKind) -> frozenset[ShowStatus]:
    if kind == EntryCheckKind.docs_precheck:
        return PRECHECK_STATUSES
    return ONSITE_STATUSES


def current_documents(docs: Iterable[D]) -> dict[DogDocumentKind, D]:
    """Действующий документ каждого вида — последний загруженный."""
    result: dict[DogDocumentKind, D] = {}
    for doc in docs:
        cur = result.get(doc.kind)
        if cur is None or doc.created_at > cur.created_at:
            result[doc.kind] = doc
    return result


def rabies_valid_for(current: Mapping[DogDocumentKind, _Doc], ref_date: date) -> bool | None:
    """None — оценить нельзя (нет ветпаспорта или даты)."""
    vp = current.get(DogDocumentKind.vet_passport)
    if vp is None or vp.valid_until is None:
        return None
    return vp.valid_until >= ref_date


def document_problems(
    current: Mapping[DogDocumentKind, _Doc], ref_date: date, class_code: str | None
) -> list[str]:
    problems: list[str] = []
    vp = current.get(DogDocumentKind.vet_passport)
    if vp is None:
        problems.append(VET_PASSPORT_MISSING)
    elif vp.valid_until is None:
        problems.append(RABIES_DATE_MISSING)
    elif vp.valid_until < ref_date:
        problems.append(RABIES_EXPIRED)
    if DogDocumentKind.pedigree not in current and DogDocumentKind.puppy_card not in current:
        problems.append(PEDIGREE_MISSING)
    if class_code == WORKING_CLASS_CODE and DogDocumentKind.working_certificate not in current:
        problems.append(WORKING_CERTIFICATE_MISSING)
    return problems


def compute_attendance_status(
    latest: Mapping[EntryCheckKind, EntryCheckResult], current: AttendanceStatus
) -> AttendanceStatus:
    """
    Статус из последних отметок каждого вида:
    1. vet или docs_onsite провалены → rejected;
    2. arrival+vet+docs_onsite пройдены → admitted;
    3. arrival пройден → arrived;
    4. иначе — исходный статус (registered/absent). Если до этого был
       arrived/admitted/rejected, а отметки отменены исправлением —
       откатываемся в registered.
    docs_precheck на статус не влияет.
    """
    arrival = latest.get(EntryCheckKind.arrival)
    vet = latest.get(EntryCheckKind.vet)
    docs = latest.get(EntryCheckKind.docs_onsite)
    if EntryCheckResult.failed in (vet, docs):
        return AttendanceStatus.rejected
    if arrival == vet == docs == EntryCheckResult.passed:
        return AttendanceStatus.admitted
    if arrival == EntryCheckResult.passed:
        return AttendanceStatus.arrived
    if current in (AttendanceStatus.registered, AttendanceStatus.absent):
        return current
    return AttendanceStatus.registered
```

- [ ] **Step 4: Run test to verify it passes**

Run: `"$PY" -m pytest tests/unit/test_checkin_rules.py -q`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add app/services/checkin_rules.py tests/unit/test_checkin_rules.py
git commit -m "feat(checkin): правила допуска и пересчёт статуса явки"
```

---

### Task 4: Документы собаки (API + ACL)

**Files:**
- Create: `app/schemas/checkin.py` (пока `DogDocumentResponse`)
- Create: `app/repositories/checkin.py` (пока функции документов и `user_has_show_access_to_dog`)
- Create: `app/services/dog_document.py`
- Create: `app/routers/dog_documents.py`
- Modify: `app/main.py` (подключить роутер после `dogs.router`)
- Test: `tests/integration/test_dog_documents.py`

**Interfaces:**
- Consumes: `services.dog._check_can_manage_dog(db, dog, requester_id, is_admin)`, `upload_quota.check_upload_quota`, `file_storage.upload_file(file, folder=...) -> (s3_key, content_type, filename, size)`, `file_storage.get_file_stream(s3_key) -> (bytes, content_type)`, `file_storage.delete_file(s3_key)`.
- Produces: `DogDocumentResponse(id, dog_id, kind, valid_until, created_at, original_filename, content_type, size_bytes, is_current)`; `checkin_repo.list_dog_documents(db, dog_ids: Iterable[UUID]) -> list[tuple[DogDocument, UploadedFile]]`; `checkin_repo.user_has_show_access_to_dog(db, dog_id, user_id) -> bool`; `dog_document.to_responses(rows) -> list[DogDocumentResponse]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_dog_documents.py
"""Интеграция: документы собаки — загрузка, список, ACL скачивания, удаление."""

from __future__ import annotations

import uuid

import pytest

import app.routers.dog_documents as router_mod
from app.models.show import ShowStaff, ShowStatus
from tests.integration.checkin_helpers import auth, make_api_user, make_world


@pytest.fixture(autouse=True)
def fake_storage(monkeypatch):
    async def _upload(file, *, folder="general"):
        await file.read()
        return f"{folder}/{uuid.uuid4()}.pdf", "application/pdf", file.filename, 4

    async def _stream(s3_key):
        return b"%PDF", "application/pdf"

    async def _delete(s3_key):
        return None

    monkeypatch.setattr(router_mod.file_storage, "upload_file", _upload)
    monkeypatch.setattr(router_mod.file_storage, "get_file_stream", _stream)
    monkeypatch.setattr(router_mod.file_storage, "delete_file", _delete)


async def _upload(client, token, dog_id, kind="vet_passport", valid_until="2030-01-01"):
    data = {"kind": kind}
    if valid_until:
        data["valid_until"] = valid_until
    return await client.post(
        f"/dogs/{dog_id}/documents",
        data=data,
        files={"file": ("vet.pdf", b"%PDF-1.4", "application/pdf")},
        headers=auth(token),
    )


async def test_owner_uploads_and_lists(client, db_session):
    owner_id, token = await make_api_user(client)
    from app.models.user import User
    owner = await db_session.get(User, owner_id)
    w = await make_world(db_session, owner=owner)

    r = await _upload(client, token, w.dog.id)
    assert r.status_code == 201, r.text
    first = r.json()
    assert first["kind"] == "vet_passport" and first["is_current"] is True

    r = await _upload(client, token, w.dog.id, valid_until="2031-01-01")
    assert r.status_code == 201

    r = await client.get(f"/dogs/{w.dog.id}/documents", headers=auth(token))
    assert r.status_code == 200
    docs = r.json()
    assert len(docs) == 2
    current = [d for d in docs if d["is_current"]]
    assert len(current) == 1 and current[0]["valid_until"] == "2031-01-01"


async def test_stranger_cannot_upload_or_list(client, db_session):
    _, stranger = await make_api_user(client)
    w = await make_world(db_session)
    assert (await _upload(client, stranger, w.dog.id)).status_code == 403
    assert (await client.get(f"/dogs/{w.dog.id}/documents", headers=auth(stranger))).status_code == 403


async def test_download_acl(client, db_session):
    owner_id, owner_token = await make_api_user(client)
    reg_id, reg_token = await make_api_user(client)
    other_reg_id, other_reg_token = await make_api_user(client)
    from app.models.user import User
    owner = await db_session.get(User, owner_id)
    w = await make_world(db_session, owner=owner)
    other = await make_world(db_session)
    db_session.add(ShowStaff(show_id=w.show.id, user_id=reg_id))
    db_session.add(ShowStaff(show_id=other.show.id, user_id=other_reg_id))
    await db_session.commit()

    doc = (await _upload(client, owner_token, w.dog.id)).json()
    url = f"/dogs/{w.dog.id}/documents/{doc['id']}/download"

    assert (await client.get(url, headers=auth(owner_token))).status_code == 200
    r = await client.get(url, headers=auth(reg_token))
    assert r.status_code == 200 and r.content == b"%PDF"
    assert (await client.get(url, headers=auth(other_reg_token))).status_code == 404

    # Выставка завершена — регистратор больше не видит документы.
    w.show.status = ShowStatus.completed
    await db_session.commit()
    assert (await client.get(url, headers=auth(reg_token))).status_code == 404


async def test_delete_document(client, db_session):
    owner_id, token = await make_api_user(client)
    from app.models.user import User
    owner = await db_session.get(User, owner_id)
    w = await make_world(db_session, owner=owner)
    doc = (await _upload(client, token, w.dog.id)).json()
    r = await client.delete(f"/dogs/{w.dog.id}/documents/{doc['id']}", headers=auth(token))
    assert r.status_code == 204
    r = await client.get(f"/dogs/{w.dog.id}/documents", headers=auth(token))
    assert r.json() == []


async def test_document_of_other_dog_404(client, db_session):
    owner_id, token = await make_api_user(client)
    from app.models.user import User
    owner = await db_session.get(User, owner_id)
    w = await make_world(db_session, owner=owner)
    w2 = await make_world(db_session, owner=owner)
    doc = (await _upload(client, token, w.dog.id)).json()
    r = await client.get(f"/dogs/{w2.dog.id}/documents/{doc['id']}/download", headers=auth(token))
    assert r.status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `"$PY" -m pytest tests/integration/test_dog_documents.py -q`
Expected: FAIL — `ModuleNotFoundError: app.routers.dog_documents`.

- [ ] **Step 3: Implement schemas, repository, service, router**

```python
# app/schemas/checkin.py
"""Схемы чек-ина и документов собаки."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict

from app.models.dog import DogDocumentKind


class DogDocumentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    dog_id: uuid.UUID
    kind: DogDocumentKind
    valid_until: date | None
    created_at: datetime
    original_filename: str
    content_type: str
    size_bytes: int
    # Действующий документ своего вида (последний загруженный).
    is_current: bool = False
```

```python
# app/repositories/checkin.py
"""Запросы чек-ина: документы собак, персонал, записи и отметки."""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dog import DogDocument
from app.models.file import UploadedFile
from app.models.show import Show, ShowEntry, ShowStaff, ShowStatus

# Документы доступны персоналу, пока выставка «живая».
_ACTIVE_SHOW_STATUSES = (
    ShowStatus.draft,
    ShowStatus.registration_open,
    ShowStatus.registration_closed,
    ShowStatus.in_progress,
)


async def list_dog_documents(
    db: AsyncSession, dog_ids: Iterable[uuid.UUID]
) -> list[tuple[DogDocument, UploadedFile]]:
    ids = list(dog_ids)
    if not ids:
        return []
    stmt = (
        select(DogDocument, UploadedFile)
        .join(UploadedFile, UploadedFile.id == DogDocument.file_id)
        .where(DogDocument.dog_id.in_(ids))
        .order_by(DogDocument.created_at.desc())
    )
    return [(d, f) for d, f in (await db.execute(stmt)).all()]


async def get_dog_document(
    db: AsyncSession, dog_id: uuid.UUID, doc_id: uuid.UUID
) -> tuple[DogDocument, UploadedFile] | None:
    stmt = (
        select(DogDocument, UploadedFile)
        .join(UploadedFile, UploadedFile.id == DogDocument.file_id)
        .where(DogDocument.id == doc_id, DogDocument.dog_id == dog_id)
    )
    row = (await db.execute(stmt)).first()
    return (row[0], row[1]) if row else None


async def user_has_show_access_to_dog(
    db: AsyncSession, dog_id: uuid.UUID, user_id: uuid.UUID
) -> bool:
    """Организатор или персонал активной выставки, где у собаки есть запись."""
    staff = exists().where(ShowStaff.show_id == Show.id, ShowStaff.user_id == user_id)
    stmt = (
        select(ShowEntry.id)
        .join(Show, Show.id == ShowEntry.show_id)
        .where(
            ShowEntry.dog_id == dog_id,
            Show.status.in_(_ACTIVE_SHOW_STATUSES),
            or_(Show.organizer_id == user_id, staff),
        )
        .limit(1)
    )
    return (await db.execute(stmt)).first() is not None
```

```python
# app/services/dog_document.py
"""
Документы собаки (ветпаспорт, родословная, ...) для допуска на выставки.

Права:
- загрузка/удаление/список — тот, кто управляет собакой (владелец,
  владелец питомника, admin) — как у фото;
- просмотр/скачивание — ещё и организатор/персонал активной выставки,
  где у собаки есть запись (им нужно сверить документы на стойке).
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dog import Dog, DogDocument, DogDocumentKind
from app.models.file import UploadedFile
from app.repositories import checkin as checkin_repo
from app.repositories import dog as dog_repo
from app.schemas.checkin import DogDocumentResponse
from app.services import checkin_rules
from app.services.dog import _check_can_manage_dog


async def get_dog_or_404(db: AsyncSession, dog_id: uuid.UUID) -> Dog:
    dog = await dog_repo.get_dog(db, dog_id)
    if dog is None:
        raise ValueError("dog_not_found")
    return dog


async def ensure_can_manage(
    db: AsyncSession, dog: Dog, user_id: uuid.UUID, is_admin: bool
) -> None:
    await _check_can_manage_dog(db, dog, user_id, is_admin)


async def can_view(db: AsyncSession, dog: Dog, user_id: uuid.UUID, is_admin: bool) -> bool:
    try:
        await _check_can_manage_dog(db, dog, user_id, is_admin)
        return True
    except ValueError:
        return await checkin_repo.user_has_show_access_to_dog(db, dog.id, user_id)


def to_responses(rows: list[tuple[DogDocument, UploadedFile]]) -> list[DogDocumentResponse]:
    current_ids = {
        d.id for d in checkin_rules.current_documents([d for d, _ in rows]).values()
    }
    return [
        DogDocumentResponse(
            id=d.id, dog_id=d.dog_id, kind=d.kind, valid_until=d.valid_until,
            created_at=d.created_at, original_filename=f.original_filename,
            content_type=f.content_type, size_bytes=f.size_bytes,
            is_current=d.id in current_ids,
        )
        for d, f in rows
    ]


async def create_document(
    db: AsyncSession,
    *,
    dog: Dog,
    user_id: uuid.UUID,
    kind: DogDocumentKind,
    valid_until: date | None,
    s3_key: str,
    content_type: str,
    filename: str,
    size_bytes: int,
) -> DogDocumentResponse:
    f = UploadedFile(
        uploaded_by=user_id, s3_key=s3_key, original_filename=filename,
        content_type=content_type, size_bytes=size_bytes,
        is_public=False,  # ПДн + ветданные — только через ACL-эндпоинт
    )
    db.add(f)
    await db.flush()
    doc = DogDocument(
        dog_id=dog.id, file_id=f.id, kind=kind, valid_until=valid_until,
        uploaded_by=user_id,
    )
    db.add(doc)
    await db.commit()
    rows = await checkin_repo.list_dog_documents(db, [dog.id])
    return next(r for r in to_responses(rows) if r.id == doc.id)
```

```python
# app/routers/dog_documents.py
"""
Документы собаки (чек-ин выставок): загрузка, список, скачивание, удаление.

Файлы приватные (files.is_public=False): публичный GET /files/{id} их
не отдаёт, скачивание — только здесь, после ACL.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import NoReturn
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.dependencies import get_current_user, is_admin
from app.models.dog import DogDocumentKind
from app.models.user import User
from app.repositories import checkin as checkin_repo
from app.schemas.checkin import DogDocumentResponse
from app.services import dog_document as svc
from app.services import file_storage, upload_quota

router = APIRouter(prefix="/dogs", tags=["dog-documents"])


def _raise_for_error(err: ValueError) -> NoReturn:
    code = str(err)
    if code.endswith("not_found"):
        raise HTTPException(404, code)
    if code == "forbidden":
        raise HTTPException(403, code)
    raise HTTPException(400, code)


@router.post(
    "/{dog_id}/documents",
    response_model=DogDocumentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Загрузить документ собаки",
)
async def upload_document(
    dog_id: uuid.UUID,
    file: UploadFile = File(...),
    kind: DogDocumentKind = Form(...),
    valid_until: date | None = Form(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        dog = await svc.get_dog_or_404(db, dog_id)
        await svc.ensure_can_manage(db, dog, user.id, is_admin(user))
    except ValueError as e:
        _raise_for_error(e)
    try:
        await upload_quota.check_upload_quota(
            db, user, declared_size_bytes=file.size or settings.max_upload_size_bytes
        )
    except upload_quota.UploadQuotaExceeded as e:
        return JSONResponse(status_code=e.status_code, content=e.body, headers=e.headers)
    s3_key, ct, filename, size_bytes = await file_storage.upload_file(
        file, folder="dog-documents"
    )
    return await svc.create_document(
        db, dog=dog, user_id=user.id, kind=kind, valid_until=valid_until,
        s3_key=s3_key, content_type=ct, filename=filename, size_bytes=size_bytes,
    )


@router.get(
    "/{dog_id}/documents",
    response_model=list[DogDocumentResponse],
    summary="Документы собаки",
)
async def list_documents(
    dog_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        dog = await svc.get_dog_or_404(db, dog_id)
    except ValueError as e:
        _raise_for_error(e)
    if not await svc.can_view(db, dog, user.id, is_admin(user)):
        raise HTTPException(403, "forbidden")
    return svc.to_responses(await checkin_repo.list_dog_documents(db, [dog.id]))


@router.delete(
    "/{dog_id}/documents/{doc_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Удалить документ собаки",
)
async def delete_document(
    dog_id: uuid.UUID,
    doc_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        dog = await svc.get_dog_or_404(db, dog_id)
        await svc.ensure_can_manage(db, dog, user.id, is_admin(user))
    except ValueError as e:
        _raise_for_error(e)
    row = await checkin_repo.get_dog_document(db, dog_id, doc_id)
    if row is None:
        raise HTTPException(404, "document_not_found")
    _doc, f = row
    s3_key = f.s3_key
    # Удаляем запись files — dog_documents уйдёт каскадом,
    # entry_checks.document_id → NULL (история отметок сохраняется).
    await db.delete(f)
    await db.commit()
    try:
        await file_storage.delete_file(s3_key)
    except Exception:  # noqa: BLE001 — сирота в MinIO не повод отдавать 500
        pass


@router.get(
    "/{dog_id}/documents/{doc_id}/download",
    summary="Скачать документ собаки",
    response_class=Response,
)
async def download_document(
    dog_id: uuid.UUID,
    doc_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        dog = await svc.get_dog_or_404(db, dog_id)
    except ValueError as e:
        _raise_for_error(e)
    # 404, а не 403: не раскрываем существование документа постороннему.
    if not await svc.can_view(db, dog, user.id, is_admin(user)):
        raise HTTPException(404, "document_not_found")
    row = await checkin_repo.get_dog_document(db, dog_id, doc_id)
    if row is None:
        raise HTTPException(404, "document_not_found")
    _doc, f = row
    body, content_type = await file_storage.get_file_stream(f.s3_key)
    safe_name = quote(f.original_filename or "document", safe="")
    return Response(
        content=body,
        media_type=content_type,
        headers={
            "Content-Disposition": f"inline; filename*=UTF-8''{safe_name}",
            "Cache-Control": "private, no-store",
        },
    )
```

`app/main.py`: `from app.routers import dog_documents` (рядом с прочими импортами роутеров) и `app.include_router(dog_documents.router)` сразу после `app.include_router(dogs.router)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `"$PY" -m pytest tests/integration/test_dog_documents.py -q`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add app/schemas/checkin.py app/repositories/checkin.py app/services/dog_document.py app/routers/dog_documents.py app/main.py tests/integration/test_dog_documents.py
git commit -m "feat(checkin): документы собаки с приватным ACL"
```

---

### Task 5: Персонал выставки и флаг чек-ина

**Files:**
- Modify: `app/schemas/checkin.py` (`ShowStaffAdd`, `ShowStaffResponse`, `CheckinSettingsUpdate`)
- Modify: `app/schemas/show.py` (`ShowResponse.checkin_enabled: bool = False`)
- Modify: `app/repositories/checkin.py` (staff-запросы)
- Create: `app/services/checkin.py` (доступ, флаг, персонал)
- Create: `app/routers/checkin.py`
- Modify: `app/main.py` (подключить `checkin.router` после `shows.router`)
- Test: `tests/integration/test_show_staff.py`

**Interfaces:**
- Produces: `checkin_svc.get_show(db, show_id) -> Show`; `checkin_svc.ensure_organizer(show, user_id, is_admin)`; `checkin_svc.ensure_desk_access(db, show, user_id, is_admin)`; `checkin_svc.ensure_enabled(show)`; `checkin_svc.set_enabled(db, show_id, user_id, is_admin, enabled) -> Show`; `checkin_svc.add_staff / remove_staff / list_staff / list_my_shows`; `checkin_svc.display_name(user) -> str`; `checkin_repo.is_staff(db, show_id, user_id) -> bool`.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_show_staff.py
"""Интеграция: флаг чек-ина и персонал выставки."""

from __future__ import annotations

from app.models.show import ShowStatus
from app.models.user import User
from tests.integration.checkin_helpers import auth, make_api_user, make_db_user, make_world


async def _world_with_api_organizer(client, db_session, **kw):
    org_id, org_token = await make_api_user(client)
    organizer = await db_session.get(User, org_id)
    w = await make_world(db_session, organizer=organizer, **kw)
    return w, org_token


async def test_toggle_checkin_enabled(client, db_session):
    w, token = await _world_with_api_organizer(client, db_session, checkin_enabled=False)
    r = await client.put(
        f"/shows/{w.show.id}/checkin/settings", json={"enabled": True}, headers=auth(token)
    )
    assert r.status_code == 200 and r.json()["checkin_enabled"] is True
    r = await client.get(f"/shows/{w.show.id}")
    assert r.json()["checkin_enabled"] is True


async def test_toggle_forbidden_for_stranger_and_locked_when_completed(client, db_session):
    w, token = await _world_with_api_organizer(client, db_session)
    _, stranger = await make_api_user(client)
    r = await client.put(
        f"/shows/{w.show.id}/checkin/settings", json={"enabled": False}, headers=auth(stranger)
    )
    assert r.status_code == 403
    w.show.status = ShowStatus.completed
    await db_session.commit()
    r = await client.put(
        f"/shows/{w.show.id}/checkin/settings", json={"enabled": False}, headers=auth(token)
    )
    assert r.status_code == 409


async def test_add_list_remove_staff_by_email_and_phone(client, db_session):
    w, token = await _world_with_api_organizer(client, db_session)
    reg_id, reg_token = await make_api_user(client)
    reg = await db_session.get(User, reg_id)
    phone_user = await make_db_user(db_session, phone="+79990001122")
    await db_session.commit()

    r = await client.post(f"/shows/{w.show.id}/staff", json={"email": reg.email.upper()}, headers=auth(token))
    assert r.status_code == 201, r.text
    r = await client.post(f"/shows/{w.show.id}/staff", json={"phone": "+79990001122"}, headers=auth(token))
    assert r.status_code == 201
    r = await client.post(f"/shows/{w.show.id}/staff", json={"email": reg.email}, headers=auth(token))
    assert r.status_code == 409

    r = await client.get(f"/shows/{w.show.id}/staff", headers=auth(token))
    assert {s["user_id"] for s in r.json()} == {str(reg_id), str(phone_user.id)}

    r = await client.get("/shows/staff/my", headers=auth(reg_token))
    assert [s["id"] for s in r.json()] == [str(w.show.id)]

    r = await client.delete(f"/shows/{w.show.id}/staff/{reg_id}", headers=auth(token))
    assert r.status_code == 204
    r = await client.get("/shows/staff/my", headers=auth(reg_token))
    assert r.json() == []


async def test_staff_validation(client, db_session):
    w, token = await _world_with_api_organizer(client, db_session)
    r = await client.post(f"/shows/{w.show.id}/staff", json={}, headers=auth(token))
    assert r.status_code == 422
    r = await client.post(
        f"/shows/{w.show.id}/staff", json={"email": "nobody_xyz@example.com"}, headers=auth(token)
    )
    assert r.status_code == 404
    _, stranger = await make_api_user(client)
    r = await client.get(f"/shows/{w.show.id}/staff", headers=auth(stranger))
    assert r.status_code == 403
```

- [ ] **Step 2: Run test to verify it fails**

Run: `"$PY" -m pytest tests/integration/test_show_staff.py -q`
Expected: FAIL — 404 на `/shows/{id}/checkin/settings`.

- [ ] **Step 3: Implement**

Добавить в `app/schemas/checkin.py`:

```python
from pydantic import EmailStr, model_validator

from app.models.show import ShowStaffRole
from app.schemas.user import E164Phone


class CheckinSettingsUpdate(BaseModel):
    enabled: bool


class ShowStaffAdd(BaseModel):
    """Ровно одно из полей: email или телефон в E.164."""

    email: EmailStr | None = None
    phone: E164Phone | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "ShowStaffAdd":
        if (self.email is None) == (self.phone is None):
            raise ValueError("Укажите email или телефон (ровно одно)")
        return self


class ShowStaffResponse(BaseModel):
    user_id: uuid.UUID
    role: ShowStaffRole
    display_name: str
    email: str | None
    phone: str | None
    created_at: datetime
```

`app/schemas/show.py`, в `ShowResponse` после полей модели:

```python
    # Включена ли регистрация прибытия (чек-ин) — см. routers/checkin.py.
    checkin_enabled: bool = False
```

Добавить в `app/repositories/checkin.py` (импорты `delete`, `selectinload`, `User`):

```python
async def is_staff(db: AsyncSession, show_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    stmt = select(ShowStaff.id).where(
        ShowStaff.show_id == show_id, ShowStaff.user_id == user_id
    )
    return (await db.execute(stmt)).first() is not None


async def list_staff(db: AsyncSession, show_id: uuid.UUID) -> list[tuple[ShowStaff, User]]:
    stmt = (
        select(ShowStaff, User)
        .join(User, User.id == ShowStaff.user_id)
        .options(selectinload(User.profile))
        .where(ShowStaff.show_id == show_id)
        .order_by(ShowStaff.created_at)
    )
    return [(s, u) for s, u in (await db.execute(stmt)).all()]


async def delete_staff(db: AsyncSession, show_id: uuid.UUID, user_id: uuid.UUID) -> int:
    res = await db.execute(
        delete(ShowStaff).where(ShowStaff.show_id == show_id, ShowStaff.user_id == user_id)
    )
    return res.rowcount or 0


async def list_staffed_shows(db: AsyncSession, user_id: uuid.UUID) -> list[Show]:
    stmt = (
        select(Show)
        .join(ShowStaff, ShowStaff.show_id == Show.id)
        .where(ShowStaff.user_id == user_id, Show.status != ShowStatus.cancelled)
        .order_by(Show.date_start.desc())
    )
    return list((await db.execute(stmt)).scalars().unique())
```

```python
# app/services/checkin.py
"""
Сервис чек-ина: флаг выставки, персонал, билет участника, стойка
(скан/поиск/отметки), сводка и очередь предпроверки.

Ошибки — ValueError("code"), маппинг в HTTP — routers/checkin.py.
"""

from __future__ import annotations

import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.show import Show, ShowStaff, ShowStaffRole, ShowStatus
from app.models.user import User
from app.repositories import checkin as repo
from app.repositories import show as show_repo
from app.repositories import user as user_repo
from app.schemas.checkin import ShowStaffResponse
from app.utils.names import full_name


def display_name(user: User | None) -> str:
    """ФИО → email → телефон: на стойке нужно хоть как-то назвать человека."""
    if user is None:
        return "—"
    return full_name(user) or user.phone or "—"


async def get_show(db: AsyncSession, show_id: uuid.UUID) -> Show:
    show = await show_repo.get_show(db, show_id)
    if show is None:
        raise ValueError("not_found")
    return show


def ensure_organizer(show: Show, user_id: uuid.UUID, is_admin: bool) -> None:
    if not is_admin and show.organizer_id != user_id:
        raise ValueError("forbidden")


async def ensure_desk_access(
    db: AsyncSession, show: Show, user_id: uuid.UUID, is_admin: bool
) -> None:
    if is_admin or show.organizer_id == user_id:
        return
    if await repo.is_staff(db, show.id, user_id):
        return
    raise ValueError("forbidden")


def ensure_enabled(show: Show) -> None:
    if not show.checkin_enabled:
        raise ValueError("checkin_disabled")


async def set_enabled(
    db: AsyncSession, show_id: uuid.UUID, user_id: uuid.UUID, is_admin: bool, enabled: bool
) -> Show:
    show = await get_show(db, show_id)
    ensure_organizer(show, user_id, is_admin)
    if show.status in (ShowStatus.completed, ShowStatus.cancelled):
        raise ValueError("show_locked")
    show.checkin_enabled = enabled
    await db.commit()
    await db.refresh(show)
    return show


def _staff_response(staff: ShowStaff, user: User) -> ShowStaffResponse:
    return ShowStaffResponse(
        user_id=user.id, role=staff.role, display_name=display_name(user),
        email=user.email, phone=user.phone, created_at=staff.created_at,
    )


async def list_staff(
    db: AsyncSession, show_id: uuid.UUID, user_id: uuid.UUID, is_admin: bool
) -> list[ShowStaffResponse]:
    show = await get_show(db, show_id)
    ensure_organizer(show, user_id, is_admin)
    return [_staff_response(s, u) for s, u in await repo.list_staff(db, show_id)]


async def add_staff(
    db: AsyncSession,
    show_id: uuid.UUID,
    requester_id: uuid.UUID,
    is_admin: bool,
    *,
    email: str | None,
    phone: str | None,
) -> ShowStaffResponse:
    show = await get_show(db, show_id)
    ensure_organizer(show, requester_id, is_admin)
    if email is not None:
        user = await user_repo.get_user_by_email(db, email.strip().lower())
    else:
        user = await user_repo.get_user_by_phone(db, phone or "")
    if user is None:
        raise ValueError("user_not_found")
    staff = ShowStaff(
        show_id=show.id, user_id=user.id, role=ShowStaffRole.registrar,
        added_by=requester_id,
    )
    db.add(staff)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise ValueError("already_staff") from None
    await db.refresh(staff)
    for s, u in await repo.list_staff(db, show.id):
        if s.id == staff.id:
            return _staff_response(s, u)
    raise ValueError("not_found")  # недостижимо: строку только что вставили


async def remove_staff(
    db: AsyncSession, show_id: uuid.UUID, requester_id: uuid.UUID, is_admin: bool,
    user_id: uuid.UUID,
) -> None:
    show = await get_show(db, show_id)
    ensure_organizer(show, requester_id, is_admin)
    if await repo.delete_staff(db, show_id, user_id) == 0:
        raise ValueError("staff_not_found")
    await db.commit()


async def list_my_shows(db: AsyncSession, user_id: uuid.UUID) -> list[Show]:
    return await repo.list_staffed_shows(db, user_id)
```

Проверить `user_repo.get_user_by_email`: если он не нормализует регистр — нормализация уже сделана в `add_staff` (`strip().lower()`); email в БД хранится в нижнем регистре (проверить `services/auth.py` регистрацию; если нет — сравнивать через `func.lower(User.email)` в новом запросе `repo.get_user_by_email_ci`).

```python
# app/routers/checkin.py
"""
Роутер чек-ина (регистрация прибытия на выставку).

Префикс /shows, как у shows.py, но отдельным модулем: shows.py уже
~600 строк. Пути /shows/staff/my и /shows/{id}/... не пересекаются с
маршрутами shows.py (у тех нет второго сегмента staff/checkin/my-ticket).
"""

from __future__ import annotations

import uuid
from typing import NoReturn

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user, is_admin
from app.models.user import User
from app.schemas.checkin import CheckinSettingsUpdate, ShowStaffAdd, ShowStaffResponse
from app.schemas.show import ShowResponse
from app.services import checkin as svc

router = APIRouter(prefix="/shows", tags=["checkin"])

_NOT_FOUND = {
    "not_found", "entry_not_found", "user_not_found", "staff_not_found",
    "document_not_found", "token_other_show", "no_entries",
}
_CONFLICT = {"checkin_disabled", "invalid_show_status", "already_staff", "show_locked"}


def _raise_for_error(err: ValueError) -> NoReturn:
    code = str(err)
    if code in _NOT_FOUND:
        raise HTTPException(404, code)
    if code == "forbidden":
        raise HTTPException(403, code)
    if code in _CONFLICT:
        raise HTTPException(409, code)
    if code == "document_mismatch":
        raise HTTPException(422, code)
    raise HTTPException(400, code)


@router.put(
    "/{show_id}/checkin/settings",
    response_model=ShowResponse,
    summary="Включить/выключить чек-ин выставки",
)
async def update_checkin_settings(
    show_id: uuid.UUID,
    body: CheckinSettingsUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return await svc.set_enabled(db, show_id, user.id, is_admin(user), body.enabled)
    except ValueError as e:
        _raise_for_error(e)


@router.get("/staff/my", response_model=list[ShowResponse], summary="Выставки, где я регистратор")
async def my_staff_shows(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return await svc.list_my_shows(db, user.id)


@router.get("/{show_id}/staff", response_model=list[ShowStaffResponse], summary="Персонал выставки")
async def list_staff(
    show_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return await svc.list_staff(db, show_id, user.id, is_admin(user))
    except ValueError as e:
        _raise_for_error(e)


@router.post(
    "/{show_id}/staff",
    response_model=ShowStaffResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Добавить регистратора по email или телефону",
)
async def add_staff(
    show_id: uuid.UUID,
    body: ShowStaffAdd,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return await svc.add_staff(
            db, show_id, user.id, is_admin(user),
            email=str(body.email) if body.email else None, phone=body.phone,
        )
    except ValueError as e:
        _raise_for_error(e)


@router.delete(
    "/{show_id}/staff/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Убрать регистратора",
)
async def remove_staff(
    show_id: uuid.UUID,
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        await svc.remove_staff(db, show_id, user.id, is_admin(user), user_id)
    except ValueError as e:
        _raise_for_error(e)
```

`app/main.py`: `from app.routers import checkin` и `app.include_router(checkin.router)` сразу после `app.include_router(shows.router)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `"$PY" -m pytest tests/integration/test_show_staff.py -q`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add app/schemas/checkin.py app/schemas/show.py app/repositories/checkin.py app/services/checkin.py app/routers/checkin.py app/main.py tests/integration/test_show_staff.py
git commit -m "feat(checkin): персонал выставки и флаг чек-ина"
```

---

### Task 6: Стойка — билет, скан, поиск, отметки, сводка, предпроверка

**Files:**
- Modify: `app/schemas/checkin.py` (карточки, отметки, билет, сводка)
- Modify: `app/repositories/checkin.py` (записи участника, поиск, отметки, сводка, очередь)
- Modify: `app/services/checkin.py` (`build_entry_cards`, `get_ticket`, `scan`, `search`, `add_checks`, `list_checks`, `summary`, `precheck_queue`)
- Modify: `app/routers/checkin.py` (эндпоинты стойки)
- Test: `tests/integration/test_checkin_desk.py`

**Interfaces:**
- Consumes: `checkin_token.make_token/parse_token`, `checkin_rules.*`, `dog_document.to_responses`.
- Produces: `EntryCard`, `ParticipantCard`, `TicketResponse`, `EntryCheckCreate`, `EntryChecksCreate`, `EntryCheckResponse`, `CheckinSummary`, `ScanRequest`; эндпоинты `GET /shows/{id}/my-ticket`, `POST /shows/{id}/checkin/scan`, `GET /shows/{id}/checkin/search`, `POST|GET /shows/{id}/entries/{entry_id}/checks`, `GET /shows/{id}/checkin/summary`, `GET /shows/{id}/checkin/precheck-queue`.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_checkin_desk.py
"""Интеграция: стойка регистрации — билет, скан, поиск, отметки, сводка."""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from app.models.dog import DogDocument, DogDocumentKind
from app.models.file import UploadedFile
from app.models.show import ShowStaff, ShowStatus
from app.models.user import User
from app.utils.checkin_token import make_token
from tests.integration.checkin_helpers import add_entry, auth, make_api_user, make_world

ADMIT = {"checks": [
    {"kind": "arrival", "result": "passed"},
    {"kind": "vet", "result": "passed"},
    {"kind": "docs_onsite", "result": "passed"},
]}


async def _setup(client, db_session, **kw):
    """Организатор (API), владелец (API), регистратор (API) + выставка."""
    org_id, org_t = await make_api_user(client)
    own_id, own_t = await make_api_user(client)
    reg_id, reg_t = await make_api_user(client)
    w = await make_world(
        db_session,
        organizer=await db_session.get(User, org_id),
        owner=await db_session.get(User, own_id),
        **kw,
    )
    db_session.add(ShowStaff(show_id=w.show.id, user_id=reg_id))
    await db_session.commit()
    return w, org_t, own_t, reg_t


async def _add_doc(db_session, w, kind, valid_until=None):
    f = UploadedFile(
        uploaded_by=w.owner.id, s3_key=f"dog-documents/{uuid.uuid4()}.pdf",
        original_filename="d.pdf", content_type="application/pdf", size_bytes=1,
        is_public=False,
    )
    db_session.add(f)
    await db_session.flush()
    d = DogDocument(dog_id=w.dog.id, file_id=f.id, kind=kind, valid_until=valid_until)
    db_session.add(d)
    await db_session.commit()
    return d


async def test_ticket_and_scan(client, db_session):
    w, org_t, own_t, reg_t = await _setup(client, db_session)
    await add_entry(db_session, w, owner=w.owner, name="Вторая")

    r = await client.get(f"/shows/{w.show.id}/my-ticket", headers=auth(own_t))
    assert r.status_code == 200, r.text
    ticket = r.json()
    assert ticket["token"].startswith("SR1.") and len(ticket["entries"]) == 2
    assert "vet_passport_missing" in ticket["entries"][0]["problems"]

    r = await client.post(
        f"/shows/{w.show.id}/checkin/scan", json={"token": ticket["token"]}, headers=auth(reg_t)
    )
    assert r.status_code == 200, r.text
    card = r.json()
    assert card["user_id"] == str(w.owner.id) and len(card["entries"]) == 2
    first = card["entries"][0]
    assert first["dog"]["microchip"] == w.dog.microchip
    assert first["attendance_status"] == "registered"


async def test_scan_errors(client, db_session):
    w, org_t, own_t, reg_t = await _setup(client, db_session)
    other = await make_world(db_session)
    r = await client.post(f"/shows/{w.show.id}/checkin/scan", json={"token": "garbage"}, headers=auth(reg_t))
    assert r.status_code == 400 and r.json()["detail"] == "invalid_token"
    r = await client.post(
        f"/shows/{w.show.id}/checkin/scan",
        json={"token": make_token(other.show.id, w.owner.id)}, headers=auth(reg_t),
    )
    assert r.status_code == 404 and r.json()["detail"] == "token_other_show"
    r = await client.post(
        f"/shows/{w.show.id}/checkin/scan",
        json={"token": make_token(w.show.id, uuid.uuid4())}, headers=auth(reg_t),
    )
    assert r.status_code == 404 and r.json()["detail"] == "no_entries"


async def test_foreign_registrar_forbidden(client, db_session):
    w, org_t, own_t, reg_t = await _setup(client, db_session)
    other, *_ = await _setup(client, db_session)
    # reg_t — регистратор выставки w, но не other.
    token = make_token(other.show.id, other.owner.id)
    r = await client.post(f"/shows/{other.show.id}/checkin/scan", json={"token": token}, headers=auth(reg_t))
    assert r.status_code == 403
    r = await client.post(f"/shows/{other.show.id}/entries/{other.entry.id}/checks", json=ADMIT, headers=auth(reg_t))
    assert r.status_code == 403
    # Запись чужой выставки через «свою» выставку — 404.
    r = await client.post(f"/shows/{w.show.id}/entries/{other.entry.id}/checks", json=ADMIT, headers=auth(reg_t))
    assert r.status_code == 404


async def test_admit_in_one_request_and_history(client, db_session):
    w, org_t, own_t, reg_t = await _setup(client, db_session)
    r = await client.post(f"/shows/{w.show.id}/entries/{w.entry.id}/checks", json=ADMIT, headers=auth(reg_t))
    assert r.status_code == 200, r.text
    assert r.json()["attendance_status"] == "admitted"
    r = await client.get(f"/shows/{w.show.id}/entries/{w.entry.id}/checks", headers=auth(reg_t))
    assert len(r.json()) == 3


async def test_correction_flips_rejected_to_admitted(client, db_session):
    w, org_t, own_t, reg_t = await _setup(client, db_session)
    reject = {"checks": [
        {"kind": "arrival", "result": "passed"},
        {"kind": "vet", "result": "failed", "comment": "нет прививки"},
    ]}
    r = await client.post(f"/shows/{w.show.id}/entries/{w.entry.id}/checks", json=reject, headers=auth(reg_t))
    assert r.json()["attendance_status"] == "rejected"
    r = await client.post(f"/shows/{w.show.id}/entries/{w.entry.id}/checks", json=ADMIT, headers=auth(reg_t))
    assert r.json()["attendance_status"] == "admitted"
    r = await client.get(f"/shows/{w.show.id}/entries/{w.entry.id}/checks", headers=auth(reg_t))
    assert len(r.json()) == 5


async def test_failed_without_comment_and_atomicity(client, db_session):
    w, org_t, own_t, reg_t = await _setup(client, db_session)
    other = await make_world(db_session)
    doc = await _add_doc(db_session, other, DogDocumentKind.vet_passport)
    r = await client.post(
        f"/shows/{w.show.id}/entries/{w.entry.id}/checks",
        json={"checks": [{"kind": "vet", "result": "failed"}]}, headers=auth(reg_t),
    )
    assert r.status_code == 422
    # Вторая отметка ссылается на документ чужой собаки → ничего не записано.
    r = await client.post(
        f"/shows/{w.show.id}/entries/{w.entry.id}/checks",
        json={"checks": [
            {"kind": "arrival", "result": "passed"},
            {"kind": "vet", "result": "passed", "document_id": str(doc.id)},
        ]},
        headers=auth(reg_t),
    )
    assert r.status_code == 422
    r = await client.get(f"/shows/{w.show.id}/entries/{w.entry.id}/checks", headers=auth(reg_t))
    assert r.json() == []


async def test_status_windows(client, db_session):
    w, org_t, own_t, reg_t = await _setup(client, db_session, status=ShowStatus.registration_open)
    r = await client.post(f"/shows/{w.show.id}/entries/{w.entry.id}/checks", json=ADMIT, headers=auth(reg_t))
    assert r.status_code == 409 and r.json()["detail"] == "invalid_show_status"
    pre = {"checks": [{"kind": "docs_precheck", "result": "passed"}]}
    r = await client.post(f"/shows/{w.show.id}/entries/{w.entry.id}/checks", json=pre, headers=auth(org_t))
    assert r.status_code == 200
    w.show.checkin_enabled = False
    await db_session.commit()
    r = await client.post(f"/shows/{w.show.id}/entries/{w.entry.id}/checks", json=pre, headers=auth(org_t))
    assert r.status_code == 409 and r.json()["detail"] == "checkin_disabled"


async def test_scan_after_checks_shows_latest_state(client, db_session):
    w, org_t, own_t, reg_t = await _setup(client, db_session)
    await _add_doc(db_session, w, DogDocumentKind.vet_passport, date.today() + timedelta(days=30))
    await client.post(f"/shows/{w.show.id}/entries/{w.entry.id}/checks", json=ADMIT, headers=auth(reg_t))
    token = (await client.get(f"/shows/{w.show.id}/my-ticket", headers=auth(own_t))).json()["token"]
    card = (await client.post(f"/shows/{w.show.id}/checkin/scan", json={"token": token}, headers=auth(reg_t))).json()
    e = card["entries"][0]
    assert e["attendance_status"] == "admitted"
    assert e["latest_checks"]["vet"]["result"] == "passed"
    assert e["rabies_valid_for_show"] is True
    assert len([d for d in e["documents"] if d["kind"] == "vet_passport"]) == 1


async def test_search_and_summary(client, db_session):
    w, org_t, own_t, reg_t = await _setup(client, db_session)
    for q in (str(w.entry.catalog_number), w.dog.microchip, w.dog.name[:4]):
        r = await client.get(f"/shows/{w.show.id}/checkin/search", params={"q": q}, headers=auth(reg_t))
        assert r.status_code == 200, (q, r.text)
        assert [c["entry_id"] for c in r.json()] == [str(w.entry.id)], q
    await client.post(f"/shows/{w.show.id}/entries/{w.entry.id}/checks", json=ADMIT, headers=auth(reg_t))
    r = await client.get(f"/shows/{w.show.id}/checkin/summary", headers=auth(reg_t))
    assert r.json() == {"total": 1, "registered": 0, "arrived": 0, "admitted": 1, "rejected": 0, "absent": 0}


async def test_precheck_queue(client, db_session):
    w, org_t, own_t, reg_t = await _setup(client, db_session, status=ShowStatus.registration_open)
    r = await client.get(f"/shows/{w.show.id}/checkin/precheck-queue", headers=auth(org_t))
    assert r.json() == []  # без документов в очереди нечего смотреть
    await _add_doc(db_session, w, DogDocumentKind.pedigree)
    r = await client.get(f"/shows/{w.show.id}/checkin/precheck-queue", headers=auth(org_t))
    assert [c["entry_id"] for c in r.json()] == [str(w.entry.id)]
    await client.post(
        f"/shows/{w.show.id}/entries/{w.entry.id}/checks",
        json={"checks": [{"kind": "docs_precheck", "result": "passed"}]}, headers=auth(org_t),
    )
    r = await client.get(f"/shows/{w.show.id}/checkin/precheck-queue", headers=auth(org_t))
    assert r.json() == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `"$PY" -m pytest tests/integration/test_checkin_desk.py -q`
Expected: FAIL — 404 на `/my-ticket`.

- [ ] **Step 3: Implement schemas**

Добавить в `app/schemas/checkin.py` (импорты `Field`, enums `AttendanceStatus`, `EntryCheckKind`, `EntryCheckResult`):

```python
class EntryCheckCreate(BaseModel):
    kind: EntryCheckKind
    result: EntryCheckResult
    comment: str | None = Field(None, max_length=1000)
    document_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _comment_on_failure(self) -> "EntryCheckCreate":
        if self.result == EntryCheckResult.failed and not (self.comment or "").strip():
            raise ValueError("Для отметки «не пройдено» нужен комментарий")
        return self


class EntryChecksCreate(BaseModel):
    checks: list[EntryCheckCreate] = Field(..., min_length=1, max_length=4)


class EntryCheckResponse(BaseModel):
    id: uuid.UUID
    kind: EntryCheckKind
    result: EntryCheckResult
    comment: str | None
    document_id: uuid.UUID | None
    performed_by: uuid.UUID | None
    performed_by_name: str | None
    created_at: datetime


class CardDog(BaseModel):
    id: uuid.UUID
    name: str
    microchip: str | None
    tattoo: str | None
    rkf_number: str | None
    avatar_file_id: uuid.UUID | None


class EntryCard(BaseModel):
    entry_id: uuid.UUID
    show_id: uuid.UUID
    catalog_number: int | None
    attendance_status: AttendanceStatus
    class_code: str
    class_name: str
    dog: CardDog
    participant_id: uuid.UUID
    participant_name: str
    participant_phone: str | None
    documents: list[DogDocumentResponse]
    rabies_valid_until: date | None
    rabies_valid_for_show: bool | None
    problems: list[str]
    latest_checks: dict[EntryCheckKind, EntryCheckResponse]


class ParticipantCard(BaseModel):
    user_id: uuid.UUID
    display_name: str
    phone: str | None
    email: str | None
    entries: list[EntryCard]


class TicketEntry(BaseModel):
    entry_id: uuid.UUID
    dog_id: uuid.UUID
    dog_name: str
    class_name: str
    catalog_number: int | None
    attendance_status: AttendanceStatus
    problems: list[str]


class TicketResponse(BaseModel):
    show_id: uuid.UUID
    token: str
    entries: list[TicketEntry]


class ScanRequest(BaseModel):
    token: str = Field(..., max_length=200)


class CheckinSummary(BaseModel):
    total: int
    registered: int
    arrived: int
    admitted: int
    rejected: int
    absent: int
```

- [ ] **Step 4: Implement repository queries**

Добавить в `app/repositories/checkin.py` (импорты `func`, `update`, `datetime`, `timezone`, `Dog`, `DogPhoto`, `ShowClass`, `EntryCheck`, `EntryCheckKind`, `AttendanceStatus`):

```python
async def list_participant_entries(
    db: AsyncSession, show_id: uuid.UUID, user_id: uuid.UUID
) -> list[ShowEntry]:
    """Записи, где человек — записавший, хендлер или владелец собаки."""
    stmt = (
        select(ShowEntry)
        .join(Dog, Dog.id == ShowEntry.dog_id)
        .where(
            ShowEntry.show_id == show_id,
            or_(
                ShowEntry.registered_by == user_id,
                ShowEntry.handler_id == user_id,
                Dog.owner_id == user_id,
            ),
        )
        .order_by(ShowEntry.catalog_number.asc().nulls_last(), ShowEntry.created_at)
    )
    return list((await db.execute(stmt)).scalars().unique())


async def search_entries(
    db: AsyncSession, show_id: uuid.UUID, q: str, limit: int = 20
) -> list[ShowEntry]:
    """
    Поиск на стойке. Цифры (до 6) — номер каталога; «+…» — телефон
    записавшего; иначе — точный чип/клеймо или подстрока клички.
    """
    q = q.strip()
    stmt = (
        select(ShowEntry)
        .join(Dog, Dog.id == ShowEntry.dog_id)
        .where(ShowEntry.show_id == show_id)
    )
    if q.isdigit() and len(q) <= 6:
        stmt = stmt.where(
            or_(ShowEntry.catalog_number == int(q), Dog.microchip == q, Dog.tattoo == q)
        )
    elif q.startswith("+"):
        stmt = stmt.join(User, User.id == ShowEntry.registered_by).where(User.phone == q)
    else:
        lowered = q.lower()
        stmt = stmt.where(
            or_(
                func.lower(Dog.microchip) == lowered,
                func.lower(Dog.tattoo) == lowered,
                Dog.name.ilike(f"%{q}%"),
            )
        )
    stmt = stmt.order_by(ShowEntry.catalog_number.asc().nulls_last()).limit(limit)
    return list((await db.execute(stmt)).scalars().unique())


async def get_entry_for_update(
    db: AsyncSession, show_id: uuid.UUID, entry_id: uuid.UUID
) -> ShowEntry | None:
    stmt = (
        select(ShowEntry)
        .where(ShowEntry.id == entry_id, ShowEntry.show_id == show_id)
        .with_for_update()
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def list_checks(db: AsyncSession, entry_ids: Iterable[uuid.UUID]) -> list[EntryCheck]:
    ids = list(entry_ids)
    if not ids:
        return []
    stmt = (
        select(EntryCheck)
        .where(EntryCheck.entry_id.in_(ids))
        .order_by(EntryCheck.created_at, EntryCheck.id)
    )
    return list((await db.execute(stmt)).scalars())


async def load_card_context(db: AsyncSession, entries: list[ShowEntry]):
    """Пакетная подгрузка всего, что нужно карточкам (без N+1)."""
    dog_ids = {e.dog_id for e in entries}
    class_ids = {e.show_class_id for e in entries}
    user_ids = {e.registered_by for e in entries}
    dogs = {d.id: d for d in (await db.execute(select(Dog).where(Dog.id.in_(dog_ids)))).scalars()}
    classes = {
        c.id: c for c in (await db.execute(select(ShowClass).where(ShowClass.id.in_(class_ids)))).scalars()
    }
    users = {
        u.id: u
        for u in (
            await db.execute(
                select(User).options(selectinload(User.profile)).where(User.id.in_(user_ids))
            )
        ).scalars()
    }
    photos = (
        await db.execute(
            select(DogPhoto)
            .where(DogPhoto.dog_id.in_(dog_ids))
            .order_by(DogPhoto.is_primary.desc(), DogPhoto.position)
        )
    ).scalars()
    avatars: dict[uuid.UUID, uuid.UUID] = {}
    for p in photos:
        avatars.setdefault(p.dog_id, p.file_id)
    docs = await list_dog_documents(db, dog_ids)
    checks = await list_checks(db, [e.id for e in entries])
    return dogs, classes, users, avatars, docs, checks


async def load_users(db: AsyncSession, user_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, User]:
    ids = [i for i in set(user_ids) if i is not None]
    if not ids:
        return {}
    stmt = select(User).options(selectinload(User.profile)).where(User.id.in_(ids))
    return {u.id: u for u in (await db.execute(stmt)).scalars()}


async def attendance_counts(db: AsyncSession, show_id: uuid.UUID) -> dict[AttendanceStatus, int]:
    stmt = (
        select(ShowEntry.attendance_status, func.count())
        .where(ShowEntry.show_id == show_id)
        .group_by(ShowEntry.attendance_status)
    )
    return {status: count for status, count in (await db.execute(stmt)).all()}


async def precheck_queue(db: AsyncSession, show_id: uuid.UUID, limit: int = 200) -> list[ShowEntry]:
    """Записи, у собак которых есть документы, но нет отметки docs_precheck."""
    has_docs = exists().where(DogDocument.dog_id == ShowEntry.dog_id)
    has_precheck = exists().where(
        EntryCheck.entry_id == ShowEntry.id, EntryCheck.kind == EntryCheckKind.docs_precheck
    )
    stmt = (
        select(ShowEntry)
        .where(ShowEntry.show_id == show_id, has_docs, ~has_precheck)
        .order_by(ShowEntry.catalog_number.asc().nulls_last(), ShowEntry.created_at)
        .limit(limit)
    )
    return list((await db.execute(stmt)).scalars())


async def mark_registered_absent(db: AsyncSession, show_id: uuid.UUID) -> int:
    """При старте выставки: все ещё не отмеченные записи → absent."""
    res = await db.execute(
        update(ShowEntry)
        .where(
            ShowEntry.show_id == show_id,
            ShowEntry.attendance_status == AttendanceStatus.registered,
        )
        .values(
            attendance_status=AttendanceStatus.absent,
            attendance_changed_at=datetime.now(timezone.utc),
        )
    )
    return res.rowcount or 0
```

- [ ] **Step 5: Implement service**

Добавить в `app/services/checkin.py` (импорты `datetime`, `timezone`, `EntryCheck`, `EntryCheckKind`, `ShowEntry`, `AttendanceStatus`, `checkin_rules as rules`, `dog_document`, `checkin_token`, схемы):

```python
def _check_response(c: EntryCheck, users: dict[uuid.UUID, User]) -> EntryCheckResponse:
    performer = users.get(c.performed_by) if c.performed_by else None
    return EntryCheckResponse(
        id=c.id, kind=c.kind, result=c.result, comment=c.comment,
        document_id=c.document_id, performed_by=c.performed_by,
        performed_by_name=display_name(performer) if performer else None,
        created_at=c.created_at,
    )


async def build_entry_cards(
    db: AsyncSession, show: Show, entries: list[ShowEntry]
) -> list[EntryCard]:
    if not entries:
        return []
    dogs, classes, users, avatars, doc_rows, checks = await repo.load_card_context(db, entries)
    performers = await repo.load_users(db, [c.performed_by for c in checks if c.performed_by])
    ref = rules.reference_date(show.date_start, show.date_end)
    cards: list[EntryCard] = []
    for e in entries:
        dog = dogs[e.dog_id]
        cls = classes[e.show_class_id]
        dog_rows = [(d, f) for d, f in doc_rows if d.dog_id == dog.id]
        current = rules.current_documents([d for d, _ in dog_rows])
        latest: dict[EntryCheckKind, EntryCheckResponse] = {}
        for c in checks:  # отсортированы по времени — последняя перезаписывает
            if c.entry_id == e.id:
                latest[c.kind] = _check_response(c, performers)
        participant = users.get(e.registered_by)
        vp = current.get(DogDocumentKind.vet_passport)
        cards.append(EntryCard(
            entry_id=e.id, show_id=e.show_id, catalog_number=e.catalog_number,
            attendance_status=e.attendance_status,
            class_code=cls.code, class_name=cls.name,
            dog=CardDog(
                id=dog.id, name=dog.name, microchip=dog.microchip, tattoo=dog.tattoo,
                rkf_number=dog.rkf_number, avatar_file_id=avatars.get(dog.id),
            ),
            participant_id=e.registered_by,
            participant_name=display_name(participant),
            participant_phone=participant.phone if participant else None,
            documents=[r for r in dog_document.to_responses(dog_rows) if r.is_current],
            rabies_valid_until=vp.valid_until if vp else None,
            rabies_valid_for_show=rules.rabies_valid_for(current, ref),
            problems=rules.document_problems(current, ref, cls.code),
            latest_checks=latest,
        ))
    return cards


async def get_ticket(db: AsyncSession, show_id: uuid.UUID, user: User) -> TicketResponse:
    show = await get_show(db, show_id)
    ensure_enabled(show)
    if show.status not in rules.TICKET_STATUSES:
        raise ValueError("invalid_show_status")
    entries = await repo.list_participant_entries(db, show.id, user.id)
    if not entries:
        raise ValueError("no_entries")
    cards = await build_entry_cards(db, show, entries)
    return TicketResponse(
        show_id=show.id,
        token=checkin_token.make_token(show.id, user.id),
        entries=[
            TicketEntry(
                entry_id=c.entry_id, dog_id=c.dog.id, dog_name=c.dog.name,
                class_name=c.class_name, catalog_number=c.catalog_number,
                attendance_status=c.attendance_status, problems=c.problems,
            )
            for c in cards
        ],
    )


async def _desk_show(
    db: AsyncSession, show_id: uuid.UUID, user: User, is_admin: bool, *, onsite: bool
) -> Show:
    show = await get_show(db, show_id)
    await ensure_desk_access(db, show, user.id, is_admin)
    ensure_enabled(show)
    if onsite and show.status not in rules.ONSITE_STATUSES:
        raise ValueError("invalid_show_status")
    return show


async def scan(
    db: AsyncSession, show_id: uuid.UUID, user: User, is_admin: bool, token: str
) -> ParticipantCard:
    show = await _desk_show(db, show_id, user, is_admin, onsite=True)
    try:
        token_show_id, participant_id = checkin_token.parse_token(token)
    except checkin_token.InvalidCheckinToken:
        raise ValueError("invalid_token") from None
    if token_show_id != show.id:
        raise ValueError("token_other_show")
    entries = await repo.list_participant_entries(db, show.id, participant_id)
    if not entries:
        raise ValueError("no_entries")
    participant = (await repo.load_users(db, [participant_id])).get(participant_id)
    return ParticipantCard(
        user_id=participant_id,
        display_name=display_name(participant),
        phone=participant.phone if participant else None,
        email=participant.email if participant else None,
        entries=await build_entry_cards(db, show, entries),
    )


async def search(
    db: AsyncSession, show_id: uuid.UUID, user: User, is_admin: bool, q: str
) -> list[EntryCard]:
    show = await _desk_show(db, show_id, user, is_admin, onsite=True)
    if len(q.strip()) < 2 and not q.strip().isdigit():
        return []
    return await build_entry_cards(db, show, await repo.search_entries(db, show.id, q))


async def add_checks(
    db: AsyncSession,
    show_id: uuid.UUID,
    entry_id: uuid.UUID,
    user: User,
    is_admin: bool,
    checks: list[EntryCheckCreate],
) -> EntryCard:
    show = await _desk_show(db, show_id, user, is_admin, onsite=False)
    for c in checks:
        if show.status not in rules.allowed_statuses_for(c.kind):
            raise ValueError("invalid_show_status")
    # FOR UPDATE: две стойки на одну собаку сериализуются, статус
    # пересчитывается по полной истории, а не по «своей» половине.
    entry = await repo.get_entry_for_update(db, show.id, entry_id)
    if entry is None:
        raise ValueError("entry_not_found")
    doc_ids = {c.document_id for c in checks if c.document_id}
    if doc_ids:
        own = {d.id for d, _ in await repo.list_dog_documents(db, [entry.dog_id])}
        if not doc_ids <= own:
            raise ValueError("document_mismatch")
    for c in checks:
        db.add(EntryCheck(
            entry_id=entry.id, kind=c.kind, result=c.result,
            comment=(c.comment or "").strip() or None,
            document_id=c.document_id, performed_by=user.id,
        ))
    await db.flush()
    latest = {c.kind: c.result for c in await repo.list_checks(db, [entry.id])}
    new_status = rules.compute_attendance_status(latest, entry.attendance_status)
    if new_status != entry.attendance_status:
        entry.attendance_status = new_status
        entry.attendance_changed_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(entry)
    return (await build_entry_cards(db, show, [entry]))[0]


async def list_entry_checks(
    db: AsyncSession, show_id: uuid.UUID, entry_id: uuid.UUID, user: User, is_admin: bool
) -> list[EntryCheckResponse]:
    show = await get_show(db, show_id)
    await ensure_desk_access(db, show, user.id, is_admin)
    entry = await show_repo.get_show_entry(db, entry_id)
    if entry is None or entry.show_id != show.id:
        raise ValueError("entry_not_found")
    checks = await repo.list_checks(db, [entry.id])
    performers = await repo.load_users(db, [c.performed_by for c in checks if c.performed_by])
    return [_check_response(c, performers) for c in reversed(checks)]


async def summary(
    db: AsyncSession, show_id: uuid.UUID, user: User, is_admin: bool
) -> CheckinSummary:
    show = await get_show(db, show_id)
    await ensure_desk_access(db, show, user.id, is_admin)
    counts = await repo.attendance_counts(db, show.id)
    return CheckinSummary(
        total=sum(counts.values()),
        **{s.value: counts.get(s, 0) for s in AttendanceStatus},
    )


async def precheck_queue(
    db: AsyncSession, show_id: uuid.UUID, user: User, is_admin: bool
) -> list[EntryCard]:
    show = await get_show(db, show_id)
    await ensure_desk_access(db, show, user.id, is_admin)
    ensure_enabled(show)
    return await build_entry_cards(db, show, await repo.precheck_queue(db, show.id))
```

Проверить сигнатуру `show_repo.get_show_entry(db, entry_id)` (`app/repositories/show.py:248`); если она принимает `(db, show_id, entry_id)` — вызвать соответственно.

- [ ] **Step 6: Implement router endpoints**

Добавить в `app/routers/checkin.py` (импорты `Query`, `Request`, `Redis`, `get_redis`, `check_rate_limit`, схемы):

```python
_SEARCH_RATE_LIMIT = 60
_SEARCH_RATE_WINDOW = 60


@router.get("/{show_id}/my-ticket", response_model=TicketResponse, summary="Мой билет (QR)")
async def my_ticket(
    show_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return await svc.get_ticket(db, show_id, user)
    except ValueError as e:
        _raise_for_error(e)


@router.post("/{show_id}/checkin/scan", response_model=ParticipantCard, summary="Скан QR на стойке")
async def scan(
    show_id: uuid.UUID,
    body: ScanRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return await svc.scan(db, show_id, user, is_admin(user), body.token)
    except ValueError as e:
        _raise_for_error(e)


@router.get("/{show_id}/checkin/search", response_model=list[EntryCard], summary="Поиск на стойке")
async def search(
    show_id: uuid.UUID,
    request: Request,
    q: str = Query(..., min_length=1, max_length=64),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    user: User = Depends(get_current_user),
):
    # Перебор ПДн (телефоны, чипы) — ограничиваем частоту.
    await check_rate_limit(request, _SEARCH_RATE_LIMIT, _SEARCH_RATE_WINDOW, redis)
    try:
        return await svc.search(db, show_id, user, is_admin(user), q)
    except ValueError as e:
        _raise_for_error(e)


@router.post(
    "/{show_id}/entries/{entry_id}/checks",
    response_model=EntryCard,
    summary="Отметки по записи (пачкой, атомарно)",
)
async def add_checks(
    show_id: uuid.UUID,
    entry_id: uuid.UUID,
    body: EntryChecksCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return await svc.add_checks(db, show_id, entry_id, user, is_admin(user), body.checks)
    except ValueError as e:
        _raise_for_error(e)


@router.get(
    "/{show_id}/entries/{entry_id}/checks",
    response_model=list[EntryCheckResponse],
    summary="История отметок записи",
)
async def list_checks(
    show_id: uuid.UUID,
    entry_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return await svc.list_entry_checks(db, show_id, entry_id, user, is_admin(user))
    except ValueError as e:
        _raise_for_error(e)


@router.get("/{show_id}/checkin/summary", response_model=CheckinSummary, summary="Сводка стойки")
async def summary(
    show_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return await svc.summary(db, show_id, user, is_admin(user))
    except ValueError as e:
        _raise_for_error(e)


@router.get(
    "/{show_id}/checkin/precheck-queue",
    response_model=list[EntryCard],
    summary="Очередь предпроверки документов",
)
async def precheck_queue(
    show_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return await svc.precheck_queue(db, show_id, user, is_admin(user))
    except ValueError as e:
        _raise_for_error(e)
```

В `_raise_for_error` добавить: `if code == "invalid_token": raise HTTPException(400, code)` (уже покрыт веткой по умолчанию — 400; оставить явной строкой для читаемости).

- [ ] **Step 7: Run tests**

Run: `"$PY" -m pytest tests/integration/test_checkin_desk.py tests/integration/test_show_staff.py tests/integration/test_dog_documents.py -q`
Expected: all passed.

- [ ] **Step 8: Commit**

```bash
git add app/schemas/checkin.py app/repositories/checkin.py app/services/checkin.py app/routers/checkin.py tests/integration/test_checkin_desk.py
git commit -m "feat(checkin): стойка регистрации — билет, скан, поиск, отметки"
```

---

### Task 7: Интеграции — неявка при старте, блокировка результатов, статус в API записей

**Files:**
- Modify: `app/services/show.py` (`change_status`: при `in_progress` + `checkin_enabled` → `checkin_repo.mark_registered_absent`)
- Modify: `app/services/result.py` (`_ensure_can_edit`: `entry_not_admitted`)
- Modify: `app/routers/results.py` (`entry_not_admitted` → 409)
- Modify: `app/schemas/show.py` (`ShowEntryResponse.attendance_status`)
- Test: `tests/integration/test_checkin_integrations.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_checkin_integrations.py
"""Интеграция чек-ина с выставкой и результатами."""

from __future__ import annotations

import pytest

from app.models.show import AttendanceStatus, ShowStatus
from app.services import result as result_svc
from app.services import show as show_svc
from tests.integration.checkin_helpers import auth, make_api_user, make_world


async def test_absent_marked_on_start(db_session):
    w = await make_world(db_session)
    await show_svc.change_status(db_session, w.show.id, w.organizer.id, False, ShowStatus.in_progress)
    await db_session.refresh(w.entry)
    assert w.entry.attendance_status == AttendanceStatus.absent
    assert w.entry.attendance_changed_at is not None


async def test_no_absent_when_checkin_disabled(db_session):
    w = await make_world(db_session, checkin_enabled=False)
    await show_svc.change_status(db_session, w.show.id, w.organizer.id, False, ShowStatus.in_progress)
    await db_session.refresh(w.entry)
    assert w.entry.attendance_status == AttendanceStatus.registered
    # Результат вносится как раньше.
    res = await result_svc.upsert_class_result(
        db_session, show_entry_id=w.entry.id, user_id=w.organizer.id, is_admin=False,
        grade_id=None, placement=None, critique=None,
    )
    assert res is not None


async def test_results_blocked_for_absent_and_rejected(db_session):
    w = await make_world(db_session, status=ShowStatus.in_progress)
    for st in (AttendanceStatus.absent, AttendanceStatus.rejected):
        w.entry.attendance_status = st
        await db_session.commit()
        with pytest.raises(ValueError, match="entry_not_admitted"):
            await result_svc.upsert_class_result(
                db_session, show_entry_id=w.entry.id, user_id=w.organizer.id,
                is_admin=False, grade_id=None, placement=None, critique=None,
            )
    w.entry.attendance_status = AttendanceStatus.registered
    await db_session.commit()
    assert await result_svc.upsert_class_result(
        db_session, show_entry_id=w.entry.id, user_id=w.organizer.id, is_admin=False,
        grade_id=None, placement=None, critique=None,
    )


async def test_late_arrival_after_absent(client, db_session):
    from app.models.show import ShowStaff
    reg_id, reg_t = await make_api_user(client)
    w = await make_world(db_session)
    db_session.add(ShowStaff(show_id=w.show.id, user_id=reg_id))
    await db_session.commit()
    await show_svc.change_status(db_session, w.show.id, w.organizer.id, False, ShowStatus.in_progress)
    r = await client.post(
        f"/shows/{w.show.id}/entries/{w.entry.id}/checks",
        json={"checks": [{"kind": "arrival", "result": "passed"}]}, headers=auth(reg_t),
    )
    assert r.status_code == 200 and r.json()["attendance_status"] == "arrived"


async def test_entries_api_exposes_attendance_status(client, db_session):
    w = await make_world(db_session)
    r = await client.get(f"/shows/{w.show.id}/entries")
    assert r.json()["items"][0]["attendance_status"] == "registered"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `"$PY" -m pytest tests/integration/test_checkin_integrations.py -q`
Expected: FAIL — статус остаётся `registered`, нет `entry_not_admitted`.

- [ ] **Step 3: Implement**

`app/services/show.py` — импорт `from app.repositories import checkin as checkin_repo`; в `change_status` сразу после блока `_assign_catalog_numbers`:

```python
    # Чек-ин: при старте выставки все не отмеченные на стойке записи
    # становятся «не явилась». Та же транзакция и тот же FOR UPDATE на
    # выставку, что и смена статуса, — гонок с отметками нет. Опоздавшего
    # регистратор отметит позже (absent → arrived).
    if target == ShowStatus.in_progress and obj.checkin_enabled:
        await checkin_repo.mark_registered_absent(db, show_id)
```

`app/services/result.py` — импорт `AttendanceStatus`; в `_ensure_can_edit` перед `return show, entry`:

```python
    # Чек-ин: не явившимся и не допущенным результат не вносится.
    # registered/arrived допустимы — опоздавших отмечают по ходу выставки.
    if show.checkin_enabled and entry.attendance_status in (
        AttendanceStatus.absent,
        AttendanceStatus.rejected,
    ):
        raise ValueError("entry_not_admitted")
```

`app/routers/results.py` — в `_raise_for_error` перед финальным `raise HTTPException(400, code)`:

```python
    if code == "entry_not_admitted":
        raise HTTPException(409, code)
```

`app/schemas/show.py` — импорт `AttendanceStatus`; в `ShowEntryResponse` после `notes`:

```python
    attendance_status: AttendanceStatus = AttendanceStatus.registered
```

- [ ] **Step 4: Run tests**

Run: `"$PY" -m pytest tests/integration/test_checkin_integrations.py tests/integration/test_result_title_revocation.py tests/integration/test_my_shows.py -q`
Expected: all passed (или skipped по сидам для старых тестов).

- [ ] **Step 5: Commit**

```bash
git add app/services/show.py app/services/result.py app/routers/results.py app/schemas/show.py tests/integration/test_checkin_integrations.py
git commit -m "feat(checkin): неявка при старте и блокировка результатов недопущенных"
```

---

### Task 8: Напоминание о документах (cron)

**Files:**
- Create: `app/services/checkin_reminders.py`
- Create: `app/templates/email/show.documents_missing.html.j2`
- Modify: `app/services/scheduler.py` (задача `remind_missing_documents` + регистрация в 10:00)
- Test: `tests/integration/test_checkin_reminders.py`

**Interfaces:**
- Produces: `collect_document_reminders(db, today: date) -> list[Reminder]`; `send_document_reminders(db, today: date) -> int`; `Reminder(user_id, show, items: list[ReminderItem])`, `ReminderItem(dog_name, problems: list[str])`.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_checkin_reminders.py
"""Интеграция: напоминание о недостающих документах за 3 дня до выставки."""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select

from app.models.notification import Notification, NotificationChannel
from app.models.show import ShowStatus
from app.services import checkin_reminders as rem
from tests.integration.checkin_helpers import add_entry, make_world

TODAY = date(2026, 10, 4)


async def _notifs(db_session, user_id):
    rows = await db_session.execute(select(Notification).where(Notification.user_id == user_id))
    return list(rows.scalars())


async def test_collect_groups_by_recipient(db_session):
    w = await make_world(db_session, date_start=TODAY + timedelta(days=3),
                         status=ShowStatus.registration_open)
    await add_entry(db_session, w, owner=w.owner, name="Вторая")
    reminders = [r for r in await rem.collect_document_reminders(db_session, TODAY) if r.show.id == w.show.id]
    assert len(reminders) == 1
    assert reminders[0].user_id == w.owner.id
    assert len(reminders[0].items) == 2


async def test_skips_other_dates_and_disabled(db_session):
    a = await make_world(db_session, date_start=TODAY + timedelta(days=4))
    b = await make_world(db_session, date_start=TODAY + timedelta(days=3), checkin_enabled=False)
    show_ids = {r.show.id for r in await rem.collect_document_reminders(db_session, TODAY)}
    assert a.show.id not in show_ids and b.show.id not in show_ids


async def test_send_is_idempotent(db_session, monkeypatch):
    monkeypatch.setattr(rem, "render_email", lambda name, ctx: ("Документы", "<p>h</p>", "t"))
    w = await make_world(db_session, date_start=TODAY + timedelta(days=3))
    await rem.send_document_reminders(db_session, TODAY)
    first = await _notifs(db_session, w.owner.id)
    assert {n.channel for n in first} == {NotificationChannel.in_app, NotificationChannel.email}
    await rem.send_document_reminders(db_session, TODAY)
    assert len(await _notifs(db_session, w.owner.id)) == len(first)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `"$PY" -m pytest tests/integration/test_checkin_reminders.py -q`
Expected: FAIL — `ModuleNotFoundError: app.services.checkin_reminders`.

- [ ] **Step 3: Implement**

```python
# app/services/checkin_reminders.py
"""
Напоминание о недостающих/просроченных документах за 3 дня до выставки.

Почему не publish_event: события проекта рассылаются по ПОДПИСКАМ
(events_handler ищет подписчиков), а здесь адресат конкретный — тот,
кто записал собаку. Поэтому как transactional-письма: in_app
Notification + письмо через outbox + WS-push.

Идемпотентность: message_id in_app-строки детерминирован
(uuid5 от выставки и получателя) + UNIQUE в notifications. Повторный
запуск (рестарт, вторая реплика) упадёт на IntegrityError, и вся
транзакция — включая письмо — откатится.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import redis as redis_state
from app.config import settings
from app.models.dog import Dog
from app.models.notification import Notification, NotificationChannel, NotificationStatus
from app.models.reference import ShowClass
from app.models.show import Show, ShowEntry, ShowStatus
from app.models.user import User
from app.repositories import checkin as checkin_repo
from app.schemas.notification import NotificationResponse
from app.services import checkin_rules as rules
from app.services.email import render_email
from app.services.email_tasks import enqueue_transactional_email

logger = logging.getLogger(__name__)

EVENT_TYPE = "show.documents_missing"
DAYS_AHEAD = 3


@dataclass
class ReminderItem:
    dog_name: str
    problems: list[str]


@dataclass
class Reminder:
    user_id: uuid.UUID
    show: Show
    items: list[ReminderItem] = field(default_factory=list)


async def collect_document_reminders(db: AsyncSession, today: date) -> list[Reminder]:
    target = today + timedelta(days=DAYS_AHEAD)
    shows = (
        await db.execute(
            select(Show).where(
                Show.checkin_enabled.is_(True),
                Show.status.in_((ShowStatus.registration_open, ShowStatus.registration_closed)),
                Show.date_start == target,
            )
        )
    ).scalars().all()
    reminders: list[Reminder] = []
    for show in shows:
        rows = (
            await db.execute(
                select(ShowEntry, Dog, ShowClass)
                .join(Dog, Dog.id == ShowEntry.dog_id)
                .join(ShowClass, ShowClass.id == ShowEntry.show_class_id)
                .where(ShowEntry.show_id == show.id)
                .order_by(ShowEntry.created_at)
            )
        ).all()
        docs = await checkin_repo.list_dog_documents(db, {dog.id for _, dog, _ in rows})
        ref = rules.reference_date(show.date_start, show.date_end)
        by_user: dict[uuid.UUID, Reminder] = defaultdict(lambda: Reminder(uuid.uuid4(), show))
        for entry, dog, cls in rows:
            current = rules.current_documents([d for d, _ in docs if d.dog_id == dog.id])
            problems = rules.document_problems(current, ref, cls.code)
            if not problems:
                continue
            reminder = by_user[entry.registered_by]
            reminder.user_id = entry.registered_by
            reminder.items.append(ReminderItem(dog.name, problems))
        reminders.extend(by_user.values())
    return reminders


async def _push(user_id: uuid.UUID, notif: Notification) -> None:
    client = redis_state.redis_client
    if client is None:
        return
    payload = NotificationResponse.model_validate(notif).model_dump(mode="json")
    try:
        await client.publish(
            f"notif:{user_id}", json.dumps({"type": "notification", "payload": payload})
        )
    except Exception as e:  # noqa: BLE001 — push best-effort, строка уже в БД
        logger.warning("documents reminder push failed for %s: %s", user_id, e)


async def send_document_reminders(db: AsyncSession, today: date) -> int:
    sent = 0
    for r in await collect_document_reminders(db, today):
        user = await db.get(User, r.user_id)
        if user is None:
            continue
        context = {
            "show_name": r.show.name,
            "date_start": r.show.date_start.strftime("%d.%m.%Y"),
            "ticket_url": f"{settings.frontend_base_url}/dashboard/my-shows/{r.show.id}/ticket",
            "dogs": [
                {"name": i.dog_name, "problems": [rules.PROBLEM_LABELS[p] for p in i.problems]}
                for i in r.items
            ],
        }
        subject, _html, _text = render_email(EVENT_TYPE, context)
        notif = Notification(
            user_id=user.id,
            event_type=EVENT_TYPE,
            channel=NotificationChannel.in_app,
            subject=subject,
            status=NotificationStatus.sent,
            sent_at=datetime.now(timezone.utc),
            message_id=uuid.uuid5(uuid.NAMESPACE_OID, f"{EVENT_TYPE}:{r.show.id}:{user.id}"),
        )
        db.add(notif)
        try:
            await db.flush()
            if user.email:
                await enqueue_transactional_email(
                    db, user_id=user.id, to_email=user.email,
                    template_name=EVENT_TYPE, context=context,
                )
            await db.commit()
        except IntegrityError:
            await db.rollback()  # уже отправляли (UNIQUE message_id)
            continue
        await db.refresh(notif)
        await _push(user.id, notif)
        sent += 1
    return sent
```

```jinja
{# app/templates/email/show.documents_missing.html.j2 #}
{% block subject %}Проверьте документы к выставке «{{ show_name }}»{% endblock %}

{% block html %}
<p>Здравствуйте!</p>
<p>До выставки <strong>{{ show_name }}</strong> ({{ date_start }}) осталось 3 дня.
  Для быстрой регистрации на месте загрузите документы собак:</p>
<ul>
{% for dog in dogs %}
  <li><strong>{{ dog.name }}</strong>
    <ul>{% for p in dog.problems %}<li>{{ p }}</li>{% endfor %}</ul>
  </li>
{% endfor %}
</ul>
<p><a href="{{ ticket_url }}">Открыть мой билет</a> — там же QR-код для стойки регистрации.</p>
{% endblock %}

{% block text %}
До выставки «{{ show_name }}» ({{ date_start }}) осталось 3 дня.
Для быстрой регистрации на месте загрузите документы собак:
{% for dog in dogs %}
- {{ dog.name }}: {{ dog.problems | join("; ") }}
{% endfor %}
Мой билет: {{ ticket_url }}
{% endblock %}
```

`app/services/scheduler.py` — импорты `from datetime import date` и `from app.services import checkin_reminders`; в `start_scheduler` перед `sched.start()`:

```python
    # Ежедневно в 10:00 — напоминание о документах за 3 дня до выставки.
    # Днём, а не ночью: письмо приходит, когда человек может его прочитать.
    sched.add_job(
        remind_missing_documents,
        CronTrigger(hour=10, minute=0),
        id="remind_missing_documents",
        replace_existing=True,
    )
```

В раздел задач:

```python
async def remind_missing_documents() -> None:
    """Напоминание о недостающих документах (чек-ин выставок)."""
    async with _scheduler_lock("remind_missing_documents") as acquired:
        if not acquired:
            return
        try:
            async with async_session_factory() as db:
                sent = await checkin_reminders.send_document_reminders(db, date.today())
            logger.info("Documents reminders sent: %d", sent)
        except Exception:  # noqa: BLE001 — cron не должен ронять шедулер
            logger.exception("remind_missing_documents failed")
```

- [ ] **Step 4: Run tests**

Run: `"$PY" -m pytest tests/integration/test_checkin_reminders.py -q`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add app/services/checkin_reminders.py app/templates/email/show.documents_missing.html.j2 app/services/scheduler.py tests/integration/test_checkin_reminders.py
git commit -m "feat(checkin): ежедневное напоминание о документах к выставке"
```

---

### Task 9: Бэкенд — линт, типы, полный прогон

- [ ] **Step 1:** `ruff check` по новым/изменённым файлам (не по всему репо — там старый долг): `ruff check app/utils/checkin_token.py app/services/checkin*.py app/services/dog_document.py app/routers/checkin.py app/routers/dog_documents.py app/repositories/checkin.py app/schemas/checkin.py tests/unit/test_checkin_*.py tests/integration/*checkin* tests/integration/test_dog_documents.py tests/integration/test_show_staff.py migrations/versions/f4a5b6c7d8e9_show_checkin.py`. Исправить замечания, кроме `B008` (FastAPI `Depends` — принятый в проекте стиль).
- [ ] **Step 2:** `pyright app/utils/checkin_token.py app/services/checkin.py app/services/checkin_rules.py app/services/checkin_reminders.py app/services/dog_document.py app/routers/checkin.py app/routers/dog_documents.py app/repositories/checkin.py` (если pyright установлен) — без новых ошибок.
- [ ] **Step 3:** Полный `pytest -q` — сравнить с базовой линией на `origin/main` (зафиксировать число падений до изменений; новых падений быть не должно).
- [ ] **Step 4:** Commit исправлений линта: `git commit -m "chore(checkin): линт и типы"`.

---

### Task 10: Фронтенд — типы, эндпоинты, actions, утилиты

**Files (repo `show-ring-frontend`, ветка `feature/show-checkin` от `origin/main`):**
- Create: `src/types/checkin.ts`
- Modify: `src/lib/axios.ts` (`endpoints.dog.documents/document/documentDownload`, `endpoints.checkin.*`)
- Modify: `src/types/show.ts` (`checkin_enabled: boolean` в `IShowItem`)
- Modify: `src/types/show-entry.ts` (`attendance_status?: AttendanceStatus`)
- Create: `src/actions/checkin.ts`
- Create: `src/sections/checkin/checkin-utils.ts`
- Test: `src/sections/checkin/__tests__/checkin-utils.test.ts`

**Interfaces:**
- Produces: типы `AttendanceStatus`, `EntryCheckKind`, `EntryCheckResult`, `DogDocumentKind`, `IDogDocument`, `IEntryCard`, `IParticipantCard`, `ITicket`, `ICheckinSummary`, `IShowStaff`, `IEntryCheck`; хуки `useDogDocuments`, `useMyTicket`, `useCheckinSummary`, `useShowStaff`, `useStaffShows`, `usePrecheckQueue`, `useEntryChecks`; функции `uploadDogDocument`, `deleteDogDocument`, `dogDocumentUrl`, `setCheckinEnabled`, `addShowStaff`, `removeShowStaff`, `scanTicket`, `searchCheckin`, `addEntryChecks`, `admitChecks()`, `rejectChecks(reason, comment)`, `arrivalOnlyChecks()`; утилиты `ATTENDANCE_COLOR`, `rabiesBadge(card)`, `isTicketAvailable(show)`, `isDeskAvailable(show)`, `scanErrorKey(detail)`.

Код — в шаге реализации (полный текст файлов пишется при исполнении задачи по этим интерфейсам; тесты ниже фиксируют поведение утилит).

- [ ] **Step 1: Write the failing test**

```ts
// src/sections/checkin/__tests__/checkin-utils.test.ts
import { it, expect, describe } from 'vitest';

import {
  rabiesBadge,
  admitChecks,
  rejectChecks,
  scanErrorKey,
  isDeskAvailable,
  isTicketAvailable,
} from '../checkin-utils';

describe('isTicketAvailable', () => {
  it('needs checkin_enabled and an active status', () => {
    expect(isTicketAvailable({ checkin_enabled: true, status: 'registration_open' })).toBe(true);
    expect(isTicketAvailable({ checkin_enabled: true, status: 'in_progress' })).toBe(true);
    expect(isTicketAvailable({ checkin_enabled: false, status: 'in_progress' })).toBe(false);
    expect(isTicketAvailable({ checkin_enabled: true, status: 'completed' })).toBe(false);
  });
});

describe('isDeskAvailable', () => {
  it('only registration_closed / in_progress', () => {
    expect(isDeskAvailable({ checkin_enabled: true, status: 'registration_closed' })).toBe(true);
    expect(isDeskAvailable({ checkin_enabled: true, status: 'registration_open' })).toBe(false);
  });
});

describe('rabiesBadge', () => {
  it('maps validity to color', () => {
    expect(rabiesBadge({ rabies_valid_for_show: true, rabies_valid_until: '2027-03-12' })).toEqual({
      color: 'success',
      date: '2027-03-12',
    });
    expect(rabiesBadge({ rabies_valid_for_show: false, rabies_valid_until: '2020-01-01' }).color).toBe(
      'error'
    );
    expect(rabiesBadge({ rabies_valid_for_show: null, rabies_valid_until: null }).color).toBe('default');
  });
});

describe('check presets', () => {
  it('admit = arrival + vet + docs_onsite passed', () => {
    expect(admitChecks().map((c) => `${c.kind}:${c.result}`)).toEqual([
      'arrival:passed',
      'vet:passed',
      'docs_onsite:passed',
    ]);
  });
  it('reject keeps arrival and fails the chosen check with comment', () => {
    expect(rejectChecks('vet', 'нет прививки')).toEqual([
      { kind: 'arrival', result: 'passed' },
      { kind: 'vet', result: 'failed', comment: 'нет прививки' },
    ]);
  });
});

describe('scanErrorKey', () => {
  it('maps backend detail codes', () => {
    expect(scanErrorKey('invalid_token')).toBe('desk.errors.invalidToken');
    expect(scanErrorKey('token_other_show')).toBe('desk.errors.otherShow');
    expect(scanErrorKey('no_entries')).toBe('desk.errors.noEntries');
    expect(scanErrorKey('whatever')).toBe('desk.errors.generic');
  });
});
```

- [ ] **Step 2:** `npx vitest run src/sections/checkin` → FAIL (модуль не найден).
- [ ] **Step 3:** Реализовать `src/types/checkin.ts`, эндпоинты, `src/actions/checkin.ts` (SWR-хуки по образцу `src/actions/show-entry.ts`: `swrOptions`, `useMemo`, `mutate` после мутаций; загрузка документа — `FormData` с `file`, `kind`, `valid_until`, повтор при 503 как в `uploadFile`), `checkin-utils.ts`.
- [ ] **Step 4:** `npx vitest run src/sections/checkin` → PASS.
- [ ] **Step 5:** Commit `feat(checkin): типы, эндпоинты и actions чек-ина`.

---

### Task 11: Фронтенд — вкладка «Документы» у собаки

**Files:**
- Create: `src/sections/dog/dog-documents.tsx`
- Modify: `src/sections/dog/view/dog-detail-view.tsx` (вкладка `documents`, видна, если `canManageDog`)
- Modify: `src/locales/langs/{ru,en}/dog.json` (ключи `documents.*`)

- [ ] **Step 1:** Компонент `DogDocuments({ dogId })`: список (`useDogDocuments`) с видом, датой загрузки, «действует до», меткой «действующий»; кнопки «Открыть» (ссылка `dogDocumentUrl` — `target="_blank"`, кука авторизации уходит сама) и «Удалить» (ConfirmDialog); форма загрузки — `TextField select` вида, `TextField type="date"` «Прививка от бешенства действительна до» (только для `vet_passport`), выбор файла (`accept="application/pdf,image/*"`), кнопка «Загрузить»; ошибки — `toast.error`.
- [ ] **Step 2:** Подключить вкладку в `DogDetailView`.
- [ ] **Step 3:** `npx tsc --noEmit` и `npx eslint src/sections/dog` — без ошибок.
- [ ] **Step 4:** Commit `feat(checkin): вкладка документов собаки`.

---

### Task 12: Фронтенд — «Мой билет»

**Files:**
- Create: `src/app/dashboard/my-shows/[id]/ticket/page.tsx`
- Create: `src/sections/checkin/view/my-ticket-view.tsx`, `src/sections/checkin/view/index.ts`
- Modify: `src/routes/paths.ts` (`dashboard.myShows.ticket(id)`)
- Modify: `src/sections/my-show/view/my-show-detail-view.tsx` (кнопка «Мой билет», если `isTicketAvailable(show)`)
- Create: `src/locales/langs/{ru,en}/checkin.json`
- Modify: `package.json` (`qrcode.react`)

- [ ] **Step 1:** `yarn add qrcode.react` (или `npm i`, по lock-файлу репозитория).
- [ ] **Step 2:** `MyTicketView({ id })`: `useMyTicket(id)`; крупный `QRCodeSVG` (`size` ~ min(80vw, 360), `level="M"`, `marginSize`), список собак с номером каталога и предупреждениями (`problems` → `t('problems.<code>')`) и ссылкой на документы собаки; кнопка «На весь экран» (Fullscreen API, fallback — модальное окно с белым фоном).
- [ ] **Step 3:** Ссылка из `MyShowDetailView`.
- [ ] **Step 4:** `npx tsc --noEmit`, `npx eslint src/sections/checkin src/sections/my-show`.
- [ ] **Step 5:** Commit `feat(checkin): страница «Мой билет» с QR`.

---

### Task 13: Фронтенд — организатор: флаг, персонал, предпроверка

**Files:**
- Create: `src/app/dashboard/shows/[id]/staff/page.tsx`, `src/app/dashboard/shows/[id]/precheck/page.tsx`
- Create: `src/sections/checkin/view/show-staff-view.tsx`, `src/sections/checkin/view/precheck-view.tsx`
- Modify: `src/routes/paths.ts` (`dashboard.shows.staff/precheck/checkin(id)`)
- Modify: `src/sections/show/view/show-edit-view.tsx` (переключатель `Switch` «Регистрация прибытия» → `setCheckinEnabled`; кнопки «Персонал», «Предпроверка», «Стойка»)

- [ ] **Step 1:** `ShowStaffView`: список (`useShowStaff`), форма «email или телефон» (одно поле; `+…` → phone, иначе email), удаление; ошибки 404 → «Пользователь не найден», 409 → «Уже в персонале».
- [ ] **Step 2:** `PrecheckView`: `usePrecheckQueue`; для каждой записи — собака, документы (ссылки на скачивание), бейджи/проблемы; «Одобрить» (`docs_precheck: passed`) и «Отклонить» (диалог с обязательным комментарием, `docs_precheck: failed`); после действия — `mutate` очереди.
- [ ] **Step 3:** Переключатель и кнопки в `ShowEditView`.
- [ ] **Step 4:** `npx tsc --noEmit`, `npx eslint src/sections/checkin src/sections/show`.
- [ ] **Step 5:** Commit `feat(checkin): персонал и предпроверка документов для организатора`.

---

### Task 14: Фронтенд — стойка регистрации

**Files:**
- Create: `src/app/dashboard/shows/[id]/checkin/page.tsx`, `src/app/dashboard/checkin/page.tsx`
- Create: `src/sections/checkin/view/checkin-desk-view.tsx`, `src/sections/checkin/view/staff-shows-view.tsx`
- Create: `src/sections/checkin/qr-scanner-dialog.tsx`, `src/sections/checkin/entry-check-card.tsx`
- Modify: `src/routes/paths.ts` (`dashboard.checkin`), `src/layouts/nav-config-dashboard.tsx` (пункт «Регистрация на выставках», `permission: 'dashboard:view'`), `src/locales/langs/{ru,en}/navbar.json`
- Modify: `package.json` (`qr-scanner`)

- [ ] **Step 1:** `yarn add qr-scanner`.
- [ ] **Step 2:** `QrScannerDialog({ open, onClose, onResult })`: полноэкранный `Dialog`; `new QrScanner(video, (r) => onResult(r.data), { preferredCamera: 'environment', highlightScanRegion: true, returnDetailedScanResult: true })`; `start()` при открытии, `stop()/destroy()` при закрытии и размонтировании; при ошибке камеры — текст «Нет доступа к камере — используйте поиск». Импорт динамический (`await import('qr-scanner')`) — модуль браузерный.
- [ ] **Step 3:** `EntryCheckCard({ card, onChanged })`: фото, кличка, номер, чип/клеймо, бейджи (предодобрено/прививка), статус (`ATTENDANCE_COLOR`), кнопки «Допустить» (`admitChecks`), «Не допустить» (диалог: причина vet/docs_onsite + комментарий → `rejectChecks`), «Только прибыла» (`arrivalOnlyChecks`), «История» (`useEntryChecks` в диалоге).
- [ ] **Step 4:** `CheckinDeskView({ id })`: шапка со счётчиками (`useCheckinSummary`, `refreshInterval: 15000`), кнопки «Сканировать» и поле поиска (debounce 400 мс), результат — `ParticipantCard` (скан) или список карточек (поиск); ошибки скана — `toast.error(t(scanErrorKey(detail)))`.
- [ ] **Step 5:** `StaffShowsView`: `useStaffShows()` + выставки, где пользователь — организатор с включённым чек-ином (через `useGetShows({...})` не нужно — достаточно staff-списка; организатор попадает на стойку из редактирования выставки); пустое состояние «Вы пока не назначены регистратором».
- [ ] **Step 6:** `npx tsc --noEmit`, `npx eslint src`, `npx vitest run`.
- [ ] **Step 7:** Commit `feat(checkin): стойка регистрации со сканером QR`.

---

### Task 15: Финальная проверка

- [ ] Бэкенд: полный `pytest -q` против базовой линии; `ruff` по новым файлам.
- [ ] Фронтенд: `npx tsc --noEmit`, `yarn lint`, `npx vitest run`, `yarn build`.
- [ ] Ручная проверка API: поднять `uvicorn` на тестовой БД, пройти сценарий «организатор включил чек-ин → добавил регистратора → владелец загрузил ветпаспорт → получил билет → регистратор отсканировал токен → допустил → старт выставки → неявка остальным».
- [ ] Финальное ревью ветки (superpowers:requesting-code-review) и отчёт пользователю.
