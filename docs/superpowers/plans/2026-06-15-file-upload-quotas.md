# File Upload Quotas Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Защитить `POST /files/upload` от перегрузок квотами по тирам пользователя (суточная частота + общий объём), backpressure-семафором к MinIO и админским CRUD лимитов.

**Architecture:** Тир пользователя (`untrusted`/`standard`/`breeder`) выводится из верификации и ролей. Квота считается в PostgreSQL (источник истины, не зависит от Redis). Лимиты тиров — редактируемая таблица `upload_quota_tiers`. Backpressure — процессный `asyncio.Semaphore` вокруг загрузки в MinIO. Слой 5 (prefetch воркера) уже закрыт, не трогаем.

**Tech Stack:** FastAPI (async), SQLAlchemy 2.0, Alembic, asyncpg, pytest + httpx (интеграция поверх реального PG/Redis).

**Spec:** `docs/superpowers/specs/2026-06-15-file-upload-quotas-design.md`

---

## File Structure

**Создаём:**
- `app/models/upload_quota.py` — модель `UploadQuotaTier` (таблица лимитов).
- `app/schemas/upload_quota.py` — `UploadQuotaTierResponse`, `UploadQuotaUpdate`.
- `app/repositories/upload_quota.py` — агрегации квот + чтение/запись лимитов + проверка владения питомником.
- `app/services/upload_quota.py` — `UploadTier`, `resolve_upload_tier`, `check_upload_quota`, исключение `UploadQuotaExceeded`, helper `_cooldown`.
- `app/routers/admin/upload_quotas.py` — `GET /admin/upload-quotas`, `PUT /admin/upload-quotas/{tier}`.
- `migrations/versions/d2b3c4d5e6f7_user_is_phone_verified.py` — колонка `users.is_phone_verified`.
- `migrations/versions/e3c4d5e6f7a8_upload_quota_tiers.py` — таблица + сид 3 строк + индекс `ix_files_uploaded_by_created`.
- `tests/unit/test_upload_tier.py` — резолвер тира + расчёт cooldown.
- `tests/integration/test_upload_quotas.py` — квоты (сервис + HTTP 429), админский CRUD, `is_phone_verified`.

**Модифицируем:**
- `app/models/user.py` — поле `is_phone_verified`.
- `app/services/otp_auth.py` — ставим `is_phone_verified=True` при успешном OTP.
- `app/config.py` — `upload_max_concurrency`, `upload_acquire_timeout_seconds`.
- `app/services/file_storage.py` — семафор вокруг `upload_file`.
- `app/routers/files.py` — вызов `check_upload_quota` до загрузки в S3.
- `app/main.py` — регистрация роутера `admin_upload_quotas`.

**Важное про тесты:** интеграционный харнесс (`tests/integration/conftest.py`) работает поверх **реального** PostgreSQL, где миграции уже применены — поэтому сид-строки `upload_quota_tiers` видны тестам. Квоту тестируем на уровне сервиса (вставляем `UploadedFile` напрямую в `db_session`) и через HTTP только путь 429 (он срабатывает ДО обращения к MinIO) — так тесты не зависят от поднятого MinIO.

---

## Task 1: Сигнал доверия `is_phone_verified`

**Files:**
- Modify: `app/models/user.py` (класс `User`, рядом с `is_email_verified`, строка ~67)
- Create: `migrations/versions/d2b3c4d5e6f7_user_is_phone_verified.py`
- Modify: `app/services/otp_auth.py` (`verify_otp_code`, перед `issue_token_pair`, ~строка 168)
- Test: `tests/integration/test_upload_quotas.py` (новый файл — здесь первый тест)

- [ ] **Step 1: Добавить колонку в модель**

В `app/models/user.py`, в классе `User` сразу после `is_email_verified` (строка 67):

```python
    is_email_verified: Mapped[bool] = mapped_column(default=False)
    # Телефон подтверждён вводом OTP-кода (основной способ верификации,
    # см. otp_auth.verify_otp_code). Вместе с is_email_verified образует
    # сигнал доверия для тиров загрузки файлов (upload_quota).
    is_phone_verified: Mapped[bool] = mapped_column(
        default=False, server_default="false"
    )
```

- [ ] **Step 2: Написать миграцию**

Создать `migrations/versions/d2b3c4d5e6f7_user_is_phone_verified.py`:

```python
"""user_is_phone_verified

Revision ID: d2b3c4d5e6f7
Revises: c1a2b3d4e5f6
Create Date: 2026-06-15 13:00:00.000000

Явный признак «телефон подтверждён OTP». До этого верифицированность
телефона была неявной (наличие phone), теперь — первоклассный флаг,
нужный тирам квот загрузки файлов (upload_quota_tiers).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d2b3c4d5e6f7"
down_revision: Union[str, Sequence[str], None] = "c1a2b3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "is_phone_verified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "is_phone_verified")
```

- [ ] **Step 3: Применить миграцию**

Run: `alembic upgrade head`
Expected: `Running upgrade c1a2b3d4e5f6 -> d2b3c4d5e6f7`

(Если head не `c1a2b3d4e5f6` — выполнить `alembic heads`, поправить `down_revision` на реальный head.)

- [ ] **Step 4: Ставить флаг при успешном OTP**

В `app/services/otp_auth.py::verify_otp_code`, после проверки `is_active` и перед `issue_token_pair` (строки ~165-170):

```python
    if not user.is_active:
        security_logger.warning("otp_login_blocked user_id=%s", user.id)
        raise OTPUserBlockedError

    # Успешный ввод OTP доказывает владение номером — фиксируем явно.
    # Идемпотентно: повторный вход не плодит лишних UPDATE.
    if not user.is_phone_verified:
        user.is_phone_verified = True

    security_logger.info("otp_login_success user_id=%s", user.id)
    return await issue_token_pair(db, user)
```

(`issue_token_pair` коммитит транзакцию — изменение атрибута уйдёт вместе с ним.)

- [ ] **Step 5: Написать интеграционный тест**

Создать `tests/integration/test_upload_quotas.py` с шапкой и первым тестом:

```python
"""
Интеграционные тесты квот загрузки файлов: сигнал доверия
is_phone_verified, суточная и объёмная квота (на уровне сервиса и HTTP),
админский CRUD лимитов.

Харнесс — реальный PostgreSQL (миграции применены, upload_quota_tiers
засеяна) + Redis, всё в транзакции с rollback (см. conftest).
"""

from __future__ import annotations

import uuid

import pytest

from app.models.file import UploadedFile
from app.models.user import RoleEnum, User, UserRole
from app.repositories.user import get_user_by_id, get_user_by_phone
from app.services import otp_auth
from app.services.upload_quota import (
    UploadQuotaExceeded,
    check_upload_quota,
)
from app.utils.security import hash_token

PASSWORD = "secret123"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _make_user(client) -> tuple[uuid.UUID, str]:
    """Регистрирует + логинит email-юзера (is_email_verified=False)."""
    email = f"uq_{uuid.uuid4().hex[:10]}@example.com"
    await client.post("/auth/register", json={"email": email, "password": PASSWORD})
    r = await client.post(
        "/auth/login",
        json={"email": email, "password": PASSWORD},
        headers={"X-Token-Delivery": "body"},
    )
    access = r.json()["access_token"]
    me = await client.get("/users/me", headers=_auth(access))
    return uuid.UUID(me.json()["id"]), access


async def _make_admin(client, db_session) -> tuple[uuid.UUID, str]:
    uid, token = await _make_user(client)
    db_session.add(UserRole(user_id=uid, role=RoleEnum.admin))
    await db_session.commit()
    return uid, token


# --- is_phone_verified -------------------------------------------------


async def test_otp_sets_is_phone_verified(db_session, test_redis):
    """Успешный verify_otp_code выставляет is_phone_verified=True."""
    phone = f"+79{uuid.uuid4().int % 10**9:09d}"
    code = "123456"
    await test_redis.set(f"otp:code:{phone}", hash_token(code))

    await otp_auth.verify_otp_code(db_session, test_redis, phone, code)

    user = await get_user_by_phone(db_session, phone)
    assert user is not None
    assert user.is_phone_verified is True
```

- [ ] **Step 6: Запустить тест**

Run: `pytest tests/integration/test_upload_quotas.py::test_otp_sets_is_phone_verified -v`
Expected: PASS (если PG/Redis не подняты — SKIP, это норма харнесса).

- [ ] **Step 7: Commit**

```bash
git add app/models/user.py migrations/versions/d2b3c4d5e6f7_user_is_phone_verified.py app/services/otp_auth.py tests/integration/test_upload_quotas.py
git commit -m "feat(uploads): is_phone_verified как явный сигнал доверия"
```

---

## Task 2: Таблица лимитов `upload_quota_tiers`

**Files:**
- Create: `app/models/upload_quota.py`
- Create: `migrations/versions/e3c4d5e6f7a8_upload_quota_tiers.py`

- [ ] **Step 1: Модель**

Создать `app/models/upload_quota.py`:

```python
"""
Модель редактируемых лимитов квот загрузки файлов (по тирам).

Источник истины для лимитов — эта таблица в PostgreSQL (не Redis):
сам счётчик квоты тоже считается в БД (upload_quota repository), держим
всё в одном месте — единая консистентность и независимость от Redis.
3 строки (untrusted/standard/breeder) засеяны миграцией; админ правит их
через PUT /admin/upload-quotas/{tier}.
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class UploadQuotaTier(Base):
    __tablename__ = "upload_quota_tiers"

    # tier — строковый PK, совпадает со значениями UploadTier enum
    # (app/services/upload_quota.py). Набор фиксирован, create/delete не
    # предусмотрены — только чтение и обновление известных строк.
    tier: Mapped[str] = mapped_column(String(32), primary_key=True)
    # Сколько загрузок в сутки (скользящее окно 24ч).
    daily_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    # Потолок суммарного объёма всех файлов юзера, в байтах.
    max_storage_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
```

- [ ] **Step 2: Миграция (таблица + сид + индекс)**

Создать `migrations/versions/e3c4d5e6f7a8_upload_quota_tiers.py`:

```python
"""upload_quota_tiers

Revision ID: e3c4d5e6f7a8
Revises: d2b3c4d5e6f7
Create Date: 2026-06-15 13:10:00.000000

Таблица редактируемых лимитов квот загрузки + сид трёх тиров + составной
индекс на files(uploaded_by, created_at) для агрегаций квоты (COUNT за
сутки и привязка к владельцу).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e3c4d5e6f7a8"
down_revision: Union[str, Sequence[str], None] = "d2b3c4d5e6f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "upload_quota_tiers",
        sa.Column("tier", sa.String(length=32), primary_key=True),
        sa.Column("daily_limit", sa.Integer(), nullable=False),
        sa.Column("max_storage_bytes", sa.BigInteger(), nullable=False),
    )
    op.bulk_insert(
        sa.table(
            "upload_quota_tiers",
            sa.column("tier", sa.String),
            sa.column("daily_limit", sa.Integer),
            sa.column("max_storage_bytes", sa.BigInteger),
        ),
        [
            {"tier": "untrusted", "daily_limit": 5, "max_storage_bytes": 52428800},
            {"tier": "standard", "daily_limit": 30, "max_storage_bytes": 524288000},
            {"tier": "breeder", "daily_limit": 200, "max_storage_bytes": 2684354560},
        ],
    )
    op.create_index(
        "ix_files_uploaded_by_created",
        "files",
        ["uploaded_by", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_files_uploaded_by_created", table_name="files")
    op.drop_table("upload_quota_tiers")
```

- [ ] **Step 3: Применить миграцию**

Run: `alembic upgrade head`
Expected: `Running upgrade d2b3c4d5e6f7 -> e3c4d5e6f7a8`

- [ ] **Step 4: Проверить сид**

Run: `python -c "import asyncio,app.database as d,sqlalchemy as sa; from app.models.upload_quota import UploadQuotaTier; print('ok')"`
Expected: печатает `ok` (модель импортируется, маппер настраивается без ошибок).

- [ ] **Step 5: Commit**

```bash
git add app/models/upload_quota.py migrations/versions/e3c4d5e6f7a8_upload_quota_tiers.py
git commit -m "feat(uploads): таблица upload_quota_tiers + сид + индекс"
```

---

## Task 3: Резолвер тира

**Files:**
- Create: `app/services/upload_quota.py` (только enum + резолвер на этом шаге)
- Test: `tests/unit/test_upload_tier.py`

- [ ] **Step 1: Написать падающий unit-тест**

Создать `tests/unit/test_upload_tier.py`:

```python
"""Юнит-тесты резолвера тира загрузки (чистая функция над User)."""

from __future__ import annotations

from app.models.user import RoleEnum, User, UserRole
from app.services.upload_quota import UploadTier, resolve_upload_tier


def _user(*, email_verified=False, phone_verified=False, roles=()):
    u = User(is_email_verified=email_verified, is_phone_verified=phone_verified)
    # roles=[] в конструкторе не передать через kwargs relationship на
    # detached-объекте безопасно — присваиваем явно списком.
    u.roles = [UserRole(role=r) for r in roles]
    return u


def test_unverified_no_kennel_is_untrusted():
    u = _user()
    assert resolve_upload_tier(u, owns_kennel=False) is UploadTier.untrusted


def test_email_verified_is_standard():
    u = _user(email_verified=True)
    assert resolve_upload_tier(u, owns_kennel=False) is UploadTier.standard


def test_phone_verified_is_standard():
    u = _user(phone_verified=True)
    assert resolve_upload_tier(u, owns_kennel=False) is UploadTier.standard


def test_breeder_role_is_breeder():
    u = _user(roles=(RoleEnum.breeder,))
    assert resolve_upload_tier(u, owns_kennel=False) is UploadTier.breeder


def test_kennel_owner_is_breeder_even_if_only_email_verified():
    u = _user(email_verified=True)
    assert resolve_upload_tier(u, owns_kennel=True) is UploadTier.breeder
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `pytest tests/unit/test_upload_tier.py -v`
Expected: FAIL с `ImportError` / `cannot import name 'UploadTier'`.

- [ ] **Step 3: Реализовать enum + резолвер**

Создать `app/services/upload_quota.py`:

```python
"""
Квоты загрузки файлов: тир пользователя и проверка лимитов.

Тир выводится из сигнала доверия (email/phone verified) и ролей.
Лимиты берутся из upload_quota_tiers (БД), счётчик квоты считается в БД —
всё независимо от Redis.
"""

from __future__ import annotations

import enum

from app.models.user import User


class UploadTier(str, enum.Enum):
    untrusted = "untrusted"
    standard = "standard"
    breeder = "breeder"


def resolve_upload_tier(user: User, *, owns_kennel: bool) -> UploadTier:
    """
    Тир пользователя для квот загрузки.

    breeder (самый свободный) — роль breeder ИЛИ владение питомником
    (создание питомника не выдаёт роль, поэтому проверяем оба признака).
    standard — верифицирован (email или телефон). untrusted — иначе.
    owns_kennel вычисляет вызывающий (repo.user_owns_kennel), чтобы
    функция оставалась чистой и юнит-тестируемой.
    """
    roles = {r.role.value for r in user.roles}
    if "breeder" in roles or owns_kennel:
        return UploadTier.breeder
    if user.is_email_verified or user.is_phone_verified:
        return UploadTier.standard
    return UploadTier.untrusted
```

- [ ] **Step 4: Запустить — убедиться, что проходит**

Run: `pytest tests/unit/test_upload_tier.py -v`
Expected: PASS (5 тестов).

- [ ] **Step 5: Commit**

```bash
git add app/services/upload_quota.py tests/unit/test_upload_tier.py
git commit -m "feat(uploads): резолвер тира пользователя"
```

---

## Task 4: Репозиторий квот

**Files:**
- Create: `app/repositories/upload_quota.py`

- [ ] **Step 1: Реализовать репозиторий**

Создать `app/repositories/upload_quota.py`:

```python
"""
Запросы для квот загрузки: агрегации по files, чтение/запись лимитов
upload_quota_tiers, проверка владения питомником.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.file import UploadedFile
from app.models.kennel import Kennel
from app.models.upload_quota import UploadQuotaTier


async def count_uploads_since(
    db: AsyncSession, user_id: UUID, since: datetime
) -> int:
    """Сколько файлов юзер загрузил с момента `since` (скользящее окно)."""
    stmt = (
        select(func.count())
        .select_from(UploadedFile)
        .where(
            UploadedFile.uploaded_by == user_id,
            UploadedFile.created_at > since,
        )
    )
    return int(await db.scalar(stmt) or 0)


async def oldest_upload_since(
    db: AsyncSession, user_id: UUID, since: datetime
) -> datetime | None:
    """created_at самой старой загрузки в окне — для расчёта cooldown."""
    stmt = (
        select(func.min(UploadedFile.created_at))
        .where(
            UploadedFile.uploaded_by == user_id,
            UploadedFile.created_at > since,
        )
    )
    return await db.scalar(stmt)


async def sum_user_storage_bytes(db: AsyncSession, user_id: UUID) -> int:
    """Суммарный объём всех файлов юзера (без окна — «сколько занимает»)."""
    stmt = (
        select(func.coalesce(func.sum(UploadedFile.size_bytes), 0))
        .where(UploadedFile.uploaded_by == user_id)
    )
    return int(await db.scalar(stmt) or 0)


async def user_owns_kennel(db: AsyncSession, user_id: UUID) -> bool:
    """Есть ли у юзера хотя бы один питомник (признак тира breeder)."""
    stmt = select(
        select(Kennel.id).where(Kennel.owner_id == user_id).exists()
    )
    return bool(await db.scalar(stmt))


async def get_tier_config(
    db: AsyncSession, tier: str
) -> UploadQuotaTier | None:
    return await db.get(UploadQuotaTier, tier)


async def list_tier_configs(db: AsyncSession) -> list[UploadQuotaTier]:
    stmt = select(UploadQuotaTier).order_by(UploadQuotaTier.tier)
    return list((await db.execute(stmt)).scalars().all())


async def update_tier_config(
    db: AsyncSession,
    tier: str,
    daily_limit: int,
    max_storage_bytes: int,
) -> UploadQuotaTier | None:
    """Обновить лимиты тира. None — если строки нет (неизвестный тир)."""
    config = await db.get(UploadQuotaTier, tier)
    if config is None:
        return None
    config.daily_limit = daily_limit
    config.max_storage_bytes = max_storage_bytes
    await db.flush()
    return config
```

- [ ] **Step 2: Проверить импорт (smoke)**

Run: `python -c "import app.repositories.upload_quota as r; print('ok')"`
Expected: печатает `ok`.

- [ ] **Step 3: Commit**

```bash
git add app/repositories/upload_quota.py
git commit -m "feat(uploads): репозиторий квот загрузки"
```

---

## Task 5: Сервис проверки квоты

**Files:**
- Modify: `app/services/upload_quota.py` (дополняем исключением, `_cooldown`, `check_upload_quota`)
- Test: `tests/unit/test_upload_tier.py` (добавляем тесты `_cooldown`)
- Test: `tests/integration/test_upload_quotas.py` (добавляем сервис-тесты квоты)

- [ ] **Step 1: Падающий unit-тест на расчёт cooldown**

Добавить в конец `tests/unit/test_upload_tier.py`:

```python
from datetime import datetime, timedelta, timezone

from app.services.upload_quota import _cooldown


def test_cooldown_from_oldest_upload():
    now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    oldest = now - timedelta(hours=20)  # выпадет из окна через 4ч
    reset_at, retry_after = _cooldown(oldest, now)
    assert reset_at == oldest + timedelta(days=1)
    assert retry_after == 4 * 3600


def test_cooldown_without_uploads_is_full_day():
    now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    reset_at, retry_after = _cooldown(None, now)
    assert reset_at == now + timedelta(days=1)
    assert retry_after == 24 * 3600
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `pytest tests/unit/test_upload_tier.py -k cooldown -v`
Expected: FAIL с `cannot import name '_cooldown'`.

- [ ] **Step 3: Дополнить сервис**

В `app/services/upload_quota.py` добавить импорты вверху и код в конец файла:

```python
# --- добавить к импортам вверху файла ---
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories import upload_quota as repo

logger = logging.getLogger(__name__)
```

```python
# --- добавить в конец файла ---


class UploadQuotaExceeded(Exception):
    """
    Квота исчерпана. Несёт готовые status_code/body/headers — роутер
    отдаёт их как JSONResponse (нужен структурированный body с инфо о
    периоде охлаждения, чего HTTPException.detail не даёт плоско).
    """

    def __init__(self, status_code: int, body: dict, headers: dict) -> None:
        self.status_code = status_code
        self.body = body
        self.headers = headers
        super().__init__(body.get("detail", "quota exceeded"))


def _cooldown(
    oldest: datetime | None, now: datetime
) -> tuple[datetime, int]:
    """
    Когда освободится слот суточного окна и сколько до этого секунд.
    reset_at = (самая старая загрузка в окне) + 24ч; если загрузок нет —
    now + 24ч (граничный случай). retry_after не отрицателен.
    """
    reset_at = (oldest + timedelta(days=1)) if oldest else (now + timedelta(days=1))
    retry_after = max(0, int((reset_at - now).total_seconds()))
    return reset_at, retry_after


async def check_upload_quota(
    db: AsyncSession, user: User, *, declared_size_bytes: int
) -> None:
    """
    Проверить квоту перед загрузкой. Поднимает UploadQuotaExceeded при
    превышении суточной частоты (429) или объёма (413). Если конфига тира
    нет (ops-ошибка, а не атака) — fail-open с warning, чтобы не ронять
    загрузку.
    """
    owns_kennel = await repo.user_owns_kennel(db, user.id)
    tier = resolve_upload_tier(user, owns_kennel=owns_kennel)
    config = await repo.get_tier_config(db, tier.value)
    if config is None:
        logger.warning("No upload quota config for tier %s — fail-open", tier.value)
        return

    now = datetime.now(timezone.utc)
    since = now - timedelta(days=1)

    used = await repo.count_uploads_since(db, user.id, since)
    if used >= config.daily_limit:
        oldest = await repo.oldest_upload_since(db, user.id, since)
        reset_at, retry_after = _cooldown(oldest, now)
        raise UploadQuotaExceeded(
            status_code=429,
            body={
                "detail": "Дневной лимит загрузок исчерпан",
                "tier": tier.value,
                "limit": config.daily_limit,
                "used": used,
                "retry_after_seconds": retry_after,
                "reset_at": reset_at.isoformat(),
            },
            headers={"Retry-After": str(retry_after)},
        )

    used_bytes = await repo.sum_user_storage_bytes(db, user.id)
    if used_bytes + declared_size_bytes > config.max_storage_bytes:
        raise UploadQuotaExceeded(
            status_code=413,
            body={
                "detail": "Превышен лимит общего объёма хранилища",
                "tier": tier.value,
                "max_storage_bytes": config.max_storage_bytes,
                "used_bytes": used_bytes,
            },
            headers={},
        )
```

- [ ] **Step 4: Запустить unit-тесты**

Run: `pytest tests/unit/test_upload_tier.py -v`
Expected: PASS (резолвер + cooldown).

- [ ] **Step 5: Добавить интеграционные сервис-тесты квоты**

Добавить в `tests/integration/test_upload_quotas.py`:

```python
async def _seed_user_in_db(
    db_session, *, email_verified=False, phone_verified=False
):
    """Создаёт юзера прямо в БД и возвращает его с загруженными ролями."""
    u = User(
        email=f"q_{uuid.uuid4().hex[:8]}@example.com",
        is_email_verified=email_verified,
        is_phone_verified=phone_verified,
        roles=[],
    )
    db_session.add(u)
    await db_session.commit()
    # get_user_by_id грузит roles через selectinload — как в проде.
    loaded = await get_user_by_id(db_session, u.id)
    assert loaded is not None
    return loaded


def _file_row(user_id, size_bytes=100):
    return UploadedFile(
        uploaded_by=user_id,
        s3_key=f"general/{uuid.uuid4()}.jpg",
        original_filename="x.jpg",
        content_type="image/jpeg",
        size_bytes=size_bytes,
    )


async def test_daily_quota_raises_429_with_cooldown(db_session):
    """untrusted (лимит 5): 5 загрузок в окне → 429 с cooldown."""
    user = await _seed_user_in_db(db_session)  # untrusted
    for _ in range(5):
        db_session.add(_file_row(user.id))
    await db_session.commit()

    with pytest.raises(UploadQuotaExceeded) as exc:
        await check_upload_quota(db_session, user, declared_size_bytes=100)

    e = exc.value
    assert e.status_code == 429
    assert e.body["tier"] == "untrusted"
    assert e.body["limit"] == 5
    assert e.body["used"] >= 5
    assert e.body["retry_after_seconds"] > 0
    assert e.headers["Retry-After"] == str(e.body["retry_after_seconds"])


async def test_storage_quota_raises_413(db_session):
    """standard (объём 500 МБ): почти полный + новый файл → 413."""
    user = await _seed_user_in_db(db_session, email_verified=True)  # standard
    db_session.add(_file_row(user.id, size_bytes=524_288_000))  # 500 МБ
    await db_session.commit()

    with pytest.raises(UploadQuotaExceeded) as exc:
        await check_upload_quota(db_session, user, declared_size_bytes=1)

    assert exc.value.status_code == 413
    assert exc.value.body["tier"] == "standard"


async def test_under_quota_passes(db_session):
    """В пределах лимитов — исключения нет."""
    user = await _seed_user_in_db(db_session, email_verified=True)  # standard
    db_session.add(_file_row(user.id))
    await db_session.commit()

    # Не должно поднять исключение.
    await check_upload_quota(db_session, user, declared_size_bytes=100)
```

- [ ] **Step 6: Запустить интеграционные тесты квоты**

Run: `pytest tests/integration/test_upload_quotas.py -k "quota or under" -v`
Expected: PASS (или SKIP без PG).

- [ ] **Step 7: Commit**

```bash
git add app/services/upload_quota.py tests/unit/test_upload_tier.py tests/integration/test_upload_quotas.py
git commit -m "feat(uploads): сервис проверки квоты с cooldown"
```

---

## Task 6: Подключить квоту к `POST /files/upload`

**Files:**
- Modify: `app/routers/files.py` (`upload_file`, импорты + тело)
- Test: `tests/integration/test_upload_quotas.py` (HTTP-тест 429)

- [ ] **Step 1: Падающий HTTP-тест**

Добавить в `tests/integration/test_upload_quotas.py`:

```python
async def test_upload_endpoint_returns_429_when_over_quota(client, db_session):
    """6-я загрузка untrusted-юзера → 429 ДО обращения к MinIO."""
    uid, token = await _make_user(client)  # untrusted, лимит 5
    for _ in range(5):
        db_session.add(_file_row(uid))
    await db_session.commit()

    files = {"file": ("x.jpg", b"\xff\xd8\xff\xe0junkbytes", "image/jpeg")}
    r = await client.post("/files/upload", files=files, headers=_auth(token))

    assert r.status_code == 429
    body = r.json()
    assert body["tier"] == "untrusted"
    assert body["limit"] == 5
    assert body["retry_after_seconds"] > 0
    assert "retry-after" in {k.lower() for k in r.headers}
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `pytest tests/integration/test_upload_quotas.py::test_upload_endpoint_returns_429_when_over_quota -v`
Expected: FAIL (сейчас вернётся 201/415/503, не 429).

- [ ] **Step 3: Подключить проверку в роутере**

В `app/routers/files.py` добавить импорты:

```python
from fastapi.responses import JSONResponse, Response
from app.config import settings
from app.services import file_storage, upload_quota
```

(строка 28 уже импортирует `Response` из `fastapi.responses` — заменить на совместную строку с `JSONResponse`; строка 39 уже импортирует `file_storage` — добавить `upload_quota`.)

В начале `upload_file`, перед вызовом `file_storage.upload_file` (строка ~92):

```python
    # Квота тира до загрузки в S3: при превышении возвращаем 429/413 и
    # не тратим запись в MinIO. declared_size — точный размер из Starlette
    # (file.size), при отсутствии — консервативно потолок одного файла.
    declared_size = file.size or settings.max_upload_size_bytes
    try:
        await upload_quota.check_upload_quota(
            db, user, declared_size_bytes=declared_size
        )
    except upload_quota.UploadQuotaExceeded as e:
        return JSONResponse(
            status_code=e.status_code, content=e.body, headers=e.headers
        )

    # Сначала валидируем + загружаем в S3, потом — пишем метаданные в БД.
    s3_key, ct, filename, size_bytes = await file_storage.upload_file(
        file, folder=folder
    )
```

- [ ] **Step 4: Запустить тест**

Run: `pytest tests/integration/test_upload_quotas.py::test_upload_endpoint_returns_429_when_over_quota -v`
Expected: PASS.

- [ ] **Step 5: Прогнать существующие файловые тесты (нет регрессий)**

Run: `pytest tests/integration/test_file_acl.py tests/integration/test_file_review_fixes.py -v`
Expected: PASS/SKIP как прежде (квота не мешает первым загрузкам — лимиты с запасом).

- [ ] **Step 6: Commit**

```bash
git add app/routers/files.py tests/integration/test_upload_quotas.py
git commit -m "feat(uploads): применять квоту в POST /files/upload"
```

---

## Task 7: Backpressure-семафор к MinIO

**Files:**
- Modify: `app/config.py` (новые настройки, рядом с `max_upload_size_bytes`, строка ~52)
- Modify: `app/services/file_storage.py` (семафор вокруг `upload_file`)

- [ ] **Step 1: Настройки конкуренции**

В `app/config.py` после `max_upload_size_bytes` (строка 52):

```python
    max_upload_size_bytes: int = 10 * 1024 * 1024  # 10 МБ
    # Backpressure к MinIO: максимум одновременных загрузок на процесс и
    # сколько ждать слот, прежде чем отдать 503. Защищает хранилище от
    # лавины параллельных put_object при всплеске трафика.
    upload_max_concurrency: int = 10
    upload_acquire_timeout_seconds: float = 5.0
```

- [ ] **Step 2: Семафор в file_storage**

В `app/services/file_storage.py` добавить импорт `asyncio` (вверху, рядом с `import logging`):

```python
import asyncio
import logging
```

После создания `_session` (строка ~73) добавить модульный семафор:

```python
# Backpressure: ограничивает число одновременных загрузок в MinIO на
# процесс. Создаётся на уровне модуля (лениво привязывается к loop'у при
# первом acquire в Python 3.10+).
_upload_semaphore = asyncio.Semaphore(settings.upload_max_concurrency)
```

Обернуть тело `upload_file` в acquire/release. Заменить сигнатуру+начало функции так, чтобы acquire шёл первым, а вся текущая логика — внутри `try/finally`:

```python
async def upload_file(
    upload: UploadFile,
    *,
    folder: str = "general",
) -> tuple[str, str, str, int]:
    """
    Стримит файл в MinIO, валидирует размер и magic bytes.
    Возвращает (s3_key, content_type, original_filename, size_bytes).

    Backpressure: захват семафора с таймаутом ДО тяжёлой работы — при
    перегрузке отдаём 503 вместо лавины соединений к MinIO.
    """
    try:
        await asyncio.wait_for(
            _upload_semaphore.acquire(),
            timeout=settings.upload_acquire_timeout_seconds,
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Хранилище временно перегружено, повторите позже",
            headers={"Retry-After": "5"},
        )
    try:
        head = await upload.read(16)
        # ... ВЕСЬ существующий код тела функции без изменений ...
        return s3_key, detected.content_type, upload.filename or "file", total
    finally:
        _upload_semaphore.release()
```

ВАЖНО: существующее тело (валидация magic bytes, чтение чанками, put_object) переносится внутрь `try` без изменения логики; сохранить все комментарии. `return` остаётся последним в `try`, `release()` — в `finally`.

- [ ] **Step 3: Прогнать файловые тесты (семафор не сломал загрузку)**

Run: `pytest tests/integration/test_file_acl.py tests/integration/test_upload_quotas.py -v`
Expected: PASS/SKIP.

- [ ] **Step 4: Проверка типов**

Run: `pyright app/services/file_storage.py app/services/upload_quota.py app/repositories/upload_quota.py`
Expected: 0 errors.

- [ ] **Step 5: Commit**

```bash
git add app/config.py app/services/file_storage.py
git commit -m "feat(uploads): backpressure-семафор к MinIO"
```

---

## Task 8: Админский CRUD лимитов

**Files:**
- Create: `app/schemas/upload_quota.py`
- Create: `app/routers/admin/upload_quotas.py`
- Modify: `app/main.py` (импорт + include_router)
- Test: `tests/integration/test_upload_quotas.py` (CRUD-тесты)

- [ ] **Step 1: Схемы**

Создать `app/schemas/upload_quota.py`:

```python
"""Схемы админского CRUD лимитов квот загрузки."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class UploadQuotaTierResponse(BaseModel):
    tier: str
    daily_limit: int
    max_storage_bytes: int

    model_config = ConfigDict(from_attributes=True)


class UploadQuotaUpdate(BaseModel):
    """Новые лимиты тира. Оба поля обязательны и положительны."""

    daily_limit: int = Field(gt=0)
    max_storage_bytes: int = Field(gt=0)
```

- [ ] **Step 2: Роутер**

Создать `app/routers/admin/upload_quotas.py`:

```python
"""
Админ-CRUD лимитов квот загрузки файлов.

Тиры фиксированы (untrusted/standard/breeder) — поэтому только list +
update известных строк (create/delete не нужны). Под ролью admin на
уровне роутера, как admin/references и переключатель feature-flags.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import require_any_role
from app.repositories import upload_quota as repo
from app.schemas.upload_quota import UploadQuotaTierResponse, UploadQuotaUpdate
from app.services.upload_quota import UploadTier

router = APIRouter(
    prefix="/admin/upload-quotas",
    tags=["admin-upload-quotas"],
    dependencies=[Depends(require_any_role("admin"))],
)


@router.get("", response_model=list[UploadQuotaTierResponse])
async def list_upload_quotas(db: AsyncSession = Depends(get_db)):
    """Лимиты всех тиров."""
    return await repo.list_tier_configs(db)


@router.put("/{tier}", response_model=UploadQuotaTierResponse)
async def update_upload_quota(
    tier: str,
    body: UploadQuotaUpdate,
    db: AsyncSession = Depends(get_db),
):
    """
    Изменить лимиты тира. Неизвестный тир → 404 (нельзя писать вне
    фиксированного набора).
    """
    if tier not in {t.value for t in UploadTier}:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="unknown tier"
        )
    updated = await repo.update_tier_config(
        db, tier, body.daily_limit, body.max_storage_bytes
    )
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="tier not found"
        )
    await db.commit()
    return updated
```

- [ ] **Step 3: Зарегистрировать роутер**

В `app/main.py` к блоку импортов admin-роутеров (строки 39-41):

```python
from app.routers.admin import references as admin_references
from app.routers.admin import analytics as admin_analytics
from app.routers.admin import moderation as admin_moderation
from app.routers.admin import upload_quotas as admin_upload_quotas
```

И рядом с `app.include_router(admin_references.router)` (строка ~171):

```python
app.include_router(admin_references.router)
app.include_router(admin_upload_quotas.router)
```

- [ ] **Step 4: Падающие CRUD-тесты**

Добавить в `tests/integration/test_upload_quotas.py`:

```python
async def test_list_quotas_requires_admin(client):
    """Не-админ → 403."""
    _, token = await _make_user(client)
    r = await client.get("/admin/upload-quotas", headers=_auth(token))
    assert r.status_code == 403


async def test_admin_lists_quota_tiers(client, db_session):
    """Админ видит три засеянных тира."""
    _, token = await _make_admin(client, db_session)
    r = await client.get("/admin/upload-quotas", headers=_auth(token))
    assert r.status_code == 200
    tiers = {row["tier"] for row in r.json()}
    assert tiers == {"untrusted", "standard", "breeder"}


async def test_admin_updates_tier_limit(client, db_session):
    """PUT меняет лимит, новое значение видно в ответе."""
    _, token = await _make_admin(client, db_session)
    r = await client.put(
        "/admin/upload-quotas/untrusted",
        json={"daily_limit": 3, "max_storage_bytes": 10485760},
        headers=_auth(token),
    )
    assert r.status_code == 200
    assert r.json()["daily_limit"] == 3
    assert r.json()["max_storage_bytes"] == 10485760


async def test_update_unknown_tier_404(client, db_session):
    _, token = await _make_admin(client, db_session)
    r = await client.put(
        "/admin/upload-quotas/nope",
        json={"daily_limit": 3, "max_storage_bytes": 10485760},
        headers=_auth(token),
    )
    assert r.status_code == 404
```

- [ ] **Step 5: Запустить CRUD-тесты**

Run: `pytest tests/integration/test_upload_quotas.py -k "quota" -v`
Expected: PASS/SKIP.

ПРИМЕЧАНИЕ про изоляцию: `test_admin_updates_tier_limit` коммитит UPDATE, но харнесс оборачивает тест во внешнюю транзакцию с rollback — изменение лимита откатится после теста, другие тесты увидят исходный сид.

- [ ] **Step 6: Финальный прогон всего модуля + типов**

Run: `pytest tests/unit/test_upload_tier.py tests/integration/test_upload_quotas.py -v`
Run: `pyright app/`
Expected: тесты PASS/SKIP; pyright 0 errors.

- [ ] **Step 7: Commit**

```bash
git add app/schemas/upload_quota.py app/routers/admin/upload_quotas.py app/main.py tests/integration/test_upload_quotas.py
git commit -m "feat(uploads): админский CRUD лимитов квот"
```

---

## Self-Review (выполнено при написании плана)

**Покрытие spec:**
- Тиры + резолвер → Task 3. `is_phone_verified` → Task 1. Таблица лимитов + сид + индекс → Task 2. Суточная квота + cooldown (429) → Task 5/6. Объёмная квота (413) → Task 5. Backpressure (слой 4) → Task 7. Админский CRUD → Task 8. Слой 5 (prefetch) — уже в коде, задачи нет (осознанно).
- Поведение при сбое Redis: квота на БД, от Redis не зависит — покрыто архитектурой (отдельного теста «Redis down» не пишем: квота вообще не трогает Redis, тест был бы тавтологией).

**Плейсхолдеры:** нет — во всех шагах реальный код/команды.

**Согласованность типов:** `check_upload_quota(db, user, *, declared_size_bytes)` и `UploadQuotaExceeded(status_code, body, headers)` одинаковы в сервисе (Task 5), роутере files (Task 6). `UploadTier` enum-значения = строковый PK таблицы и сид (`untrusted/standard/breeder`) во всех задачах. `resolve_upload_tier(user, *, owns_kennel)` совпадает в юнит-тесте (Task 3) и сервисе (Task 5). Имена repo-функций (`count_uploads_since`, `oldest_upload_since`, `sum_user_storage_bytes`, `user_owns_kennel`, `get_tier_config`, `list_tier_configs`, `update_tier_config`) совпадают между Task 4 и потребителями.

**Гранулярность:** каждая задача — рабочий, тестируемый инкремент с TDD-циклом и коммитом.
