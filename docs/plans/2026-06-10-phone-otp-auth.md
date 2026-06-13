# Phone OTP Auth — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Вход и регистрация по номеру телефона с одноразовым SMS-кодом (OTP): два эндпоинта `POST /auth/send-code` и `POST /auth/verify-code`, выдающие ту же пару JWT access + refresh, что и существующий email-логин.

**Architecture:** Новый способ входа встраивается в существующие слои: роутер `app/routers/auth.py` → сервис `app/services/otp_auth.py` (бизнес-логика OTP) → репозиторий `app/repositories/user.py` + Redis (хранение кода, cooldown, счётчик попыток). Интеграция с SMS-провайдерами — отдельный слой `app/services/sms.py` с абстрактным `SMSProvider` (Mock для dev, sms.ru как пример реального), внедряется через `Depends(get_sms_provider)`. Выдача токенов переиспользует существующую инфраструктуру (`create_access_token`, таблица `refresh_tokens`, rotation в `/auth/refresh`).

**Tech Stack:** FastAPI (async), SQLAlchemy 2.0 + asyncpg, Alembic, Pydantic v2, redis-py (async), python-jose (JWT), httpx (реальный SMS-провайдер).

---

## Решения, зафиксированные до старта

1. **Префикс URL.** В ТЗ указан `/api/v1/auth/...`, но весь существующий бэкенд монтирует роутеры без версионного префикса (`/auth/login`, `/auth/refresh` — см. `app/main.py:156-190`). Единообразие бэкенда важнее буквы ТЗ → эндпоинты будут `/auth/send-code` и `/auth/verify-code`. Введение `/api/v1` для всего API — отдельная задача, не этого плана.
2. **Пользователь без email и пароля.** Сейчас `users.email` и `users.hashed_password` — NOT NULL. Телефонный пользователь не имеет ни того, ни другого → обе колонки становятся nullable + CHECK-констрейнт `email IS NOT NULL OR phone IS NOT NULL` (у пользователя обязан быть хотя бы один идентификатор). Все места, где `hashed_password` разыменовывается (`login_user`, `request_email_change`, `change_password`), получают guard.
3. **Хранение кода.** В Redis кладётся не сам код, а его SHA-256 (`hash_token`) — дамп Redis не отдаёт коды в открытом виде. Сравнение — `secrets.compare_digest`.
4. **Ключи Redis** (все per-phone):
   - `otp:cooldown:{phone}` — маркер «SMS уже отправлено», `SET NX EX 60` (атомарно);
   - `otp:code:{phone}` — SHA-256 кода, TTL 300 с;
   - `otp:attempts:{phone}` — счётчик попыток ввода (INCR), TTL как у кода;
   - `otp:daily:{phone}` — суточный счётчик отправок, TTL 86400 с (анти SMS-pumping: SMS стоят денег, перебор номеров с одного IP — известная атака).
5. **Коды ошибок** (по ТЗ): 429 — повторная отправка раньше cooldown / суточный лимит; 401 — кода нет / истёк / сожжён после 3 неверных попыток; 400 — неверный код (пока попытки остались); 502 — сбой SMS-провайдера; 422 — невалидный формат номера (стандарт Pydantic/FastAPI).
6. **Доставка refresh-токена.** По умолчанию — оба токена в теле ответа (как существующий `/auth/login`; мобильному приложению cookie не нужен). Флаг `AUTH_REFRESH_COOKIE=true` переключает веб-режим: refresh уходит в httpOnly-cookie (`secure`, `samesite=strict`, `path=/auth`), в теле `refresh_token: null`. `/auth/refresh` и `/auth/logout` учатся читать refresh из cookie как fallback.
7. **Анти-enumeration.** `/auth/send-code` отвечает одинаково для нового и существующего номера. Существование аккаунта не раскрывается.
8. **Осознанное ограничение.** Телефонный пользователь без пароля не может сменить email/пароль (эти операции требуют re-auth по текущему паролю). Flow «установить пароль по OTP» — будущая задача, в план не входит.

## Структура файлов

| Файл | Действие | Ответственность |
|---|---|---|
| `app/config.py` | изменить | настройки SMS/OTP/cookie |
| `app/models/user.py` | изменить | колонка `phone`, nullable `email`/`hashed_password`, CHECK |
| `migrations/versions/<new>_phone_otp_auth.py` | создать | миграция схемы |
| `app/repositories/user.py` | изменить | `get_user_by_phone`, `create_user_by_phone` |
| `app/schemas/user.py` | изменить | `PhoneSendCodeRequest`, `PhoneVerifyCodeRequest`, nullable-поля в ответах |
| `app/services/sms.py` | создать | абстракция SMS-провайдера + DI |
| `app/services/otp_auth.py` | создать | бизнес-логика send/verify OTP |
| `app/services/auth.py` | изменить | guard для юзеров без пароля, общий `issue_token_pair` |
| `app/routers/auth.py` | изменить | эндпоинты `/send-code`, `/verify-code`, cookie-режим |
| `tests/services/test_sms_provider.py` | создать | unit: SMS-слой |
| `tests/services/test_otp_auth.py` | создать | unit: OTP-сервис (Redis/repo замоканы) |
| `tests/services/test_auth_security.py` | изменить | unit: guard юзера без пароля |
| `tests/unit/test_phone_schemas.py` | создать | unit: E.164-валидатор |
| `tests/integration/test_phone_auth_flow.py` | создать | интеграция: полный флоу через HTTP + PG + Redis |

---

### Task 1: Настройки OTP и SMS в конфиге

**Files:**
- Modify: `app/config.py` (после блока «Этап 19», строка ~71)

- [ ] **Step 1: Добавить настройки**

В `class Settings`, после `frontend_base_url`:

```python
    # --- Phone OTP auth ---
    # Провайдер SMS: "mock" — пишет в лог вместо отправки (dev/тесты),
    # "smsru" — реальный HTTP-провайдер sms.ru (нужен SMS_API_KEY).
    sms_provider: str = "mock"
    sms_api_key: str | None = None
    # Длина кода и TTL. 6 цифр / 5 минут — индустриальный стандарт.
    otp_code_length: int = 6
    otp_code_ttl_seconds: int = 300
    # Пауза между отправками на один номер (анти-спам по конкретному номеру).
    otp_send_cooldown_seconds: int = 60
    # Попыток ввода кода до его сжигания.
    otp_max_attempts: int = 3
    # Потолок SMS на номер в сутки — защита от SMS-pumping (каждое SMS
    # стоит денег; атакующий перебором номеров может сжечь бюджет).
    otp_daily_limit: int = 10
    # Доставка refresh-токена: False — в теле ответа (текущее поведение
    # /auth/login, удобно мобильному приложению); True — httpOnly-cookie
    # для веб-фронта, в теле refresh_token=null.
    auth_refresh_cookie: bool = False
```

- [ ] **Step 2: Убедиться, что ничего не сломано**

Run: `python -m pytest tests/unit -q`
Expected: все тесты зелёные (настройки имеют дефолты, .env менять не нужно).

- [ ] **Step 3: Commit**

```bash
git add app/config.py
git commit -m "feat(auth): OTP/SMS settings for phone login"
```

---

### Task 2: Модель User — телефон, nullable email/пароль, миграция

**Files:**
- Modify: `app/models/user.py:34-57`
- Create: `migrations/versions/b4f8c2d91a37_phone_otp_auth.py`

- [ ] **Step 1: Изменить модель**

В `app/models/user.py` импортировать `CheckConstraint` (добавить в существующий импорт из `sqlalchemy`) и заменить начало класса `User`:

```python
class User(Base, TimestampMixin):
    __tablename__ = "users"
    # Phone-OTP: у пользователя обязан быть хотя бы один идентификатор —
    # email (классическая регистрация) или phone (вход по SMS-коду).
    __table_args__ = (
        CheckConstraint(
            "email IS NOT NULL OR phone IS NOT NULL",
            name="ck_users_email_or_phone",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Phone-OTP: email стал nullable — телефонные пользователи живут без него.
    email: Mapped[str | None] = mapped_column(
        String(255), unique=True, index=True, nullable=True
    )
    # E.164: "+" и до 15 цифр → 16 символов достаточно.
    phone: Mapped[str | None] = mapped_column(
        String(16), unique=True, index=True, nullable=True
    )
```

и сделать пароль nullable (телефонные пользователи аутентифицируются кодом):

```python
    hashed_password: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
```

Остальные поля и docstring-комментарии класса не трогать.

- [ ] **Step 2: Написать миграцию**

Create `migrations/versions/b4f8c2d91a37_phone_otp_auth.py`:

```python
"""phone otp auth: users.phone, nullable email/password

Revision ID: b4f8c2d91a37
Revises: f2a3b4c5d6e7
Create Date: 2026-06-10
"""
import sqlalchemy as sa
from alembic import op

revision = "b4f8c2d91a37"
down_revision = "f2a3b4c5d6e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("phone", sa.String(16), nullable=True))
    op.create_index("ix_users_phone", "users", ["phone"], unique=True)
    op.alter_column(
        "users", "email", existing_type=sa.String(255), nullable=True
    )
    op.alter_column(
        "users",
        "hashed_password",
        existing_type=sa.String(255),
        nullable=True,
    )
    op.create_check_constraint(
        "ck_users_email_or_phone",
        "users",
        "email IS NOT NULL OR phone IS NOT NULL",
    )


def downgrade() -> None:
    # ВНИМАНИЕ: downgrade предполагает, что телефонных пользователей
    # (email IS NULL или hashed_password IS NULL) в БД нет — иначе
    # alter_column на NOT NULL упадёт. Это сознательно: молча удалять
    # пользователей миграция не должна.
    op.drop_constraint("ck_users_email_or_phone", "users", type_="check")
    op.alter_column(
        "users",
        "hashed_password",
        existing_type=sa.String(255),
        nullable=False,
    )
    op.alter_column(
        "users", "email", existing_type=sa.String(255), nullable=False
    )
    op.drop_index("ix_users_phone", table_name="users")
    op.drop_column("users", "phone")
```

Перед коммитом сверить `down_revision` с реальным head: `alembic heads` → должно показать `f2a3b4c5d6e7`. Если head другой (появились новые миграции) — подставить актуальный.

- [ ] **Step 3: Применить и проверить**

Run: `alembic upgrade head`
Expected: `Running upgrade f2a3b4c5d6e7 -> b4f8c2d91a37` без ошибок.

Run: `alembic downgrade -1; alembic upgrade head`
Expected: обе команды проходят (downgrade обратим на чистых данных).

- [ ] **Step 4: Commit**

```bash
git add app/models/user.py migrations/versions/b4f8c2d91a37_phone_otp_auth.py
git commit -m "feat(auth): users.phone column, nullable email/password + CHECK"
```

---

### Task 3: Репозиторий — поиск и создание пользователя по телефону

**Files:**
- Modify: `app/repositories/user.py` (после `create_user`, строка ~30)

- [ ] **Step 1: Добавить функции**

```python
async def get_user_by_phone(db: AsyncSession, phone: str) -> User | None:
    stmt = (
        select(User)
        .where(User.phone == phone)
        .options(selectinload(User.roles))
    )

    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def create_user_by_phone(db: AsyncSession, phone: str) -> User:
    # roles=[] инициализирует коллекцию сразу: после flush объект
    # persistent, и первое обращение к user.roles иначе запустило бы
    # lazy load — в async-сессии это MissingGreenlet-ошибка.
    user = User(phone=phone, roles=[])
    db.add(user)
    await db.flush()
    return user
```

- [ ] **Step 2: Smoke-проверка типов**

Run: `python -c "from app.repositories.user import get_user_by_phone, create_user_by_phone"`
Expected: без ошибок импорта. Поведение покроют unit-тесты сервиса (Task 7–8) и интеграция (Task 10).

- [ ] **Step 3: Commit**

```bash
git add app/repositories/user.py
git commit -m "feat(auth): user repo lookup/create by phone"
```

---

### Task 4: Pydantic-схемы — E.164 и запросы OTP

**Files:**
- Modify: `app/schemas/user.py`
- Test: `tests/unit/test_phone_schemas.py`

- [ ] **Step 1: Написать падающие тесты**

Create `tests/unit/test_phone_schemas.py`:

```python
"""Валидация телефонных схем: E.164 и формат кода."""

import pytest
from pydantic import ValidationError

from app.schemas.user import PhoneSendCodeRequest, PhoneVerifyCodeRequest


@pytest.mark.parametrize(
    "phone",
    ["+79991234567", "+12025550123", "+442071838750"],
)
def test_valid_e164_accepted(phone):
    assert PhoneSendCodeRequest(phone=phone).phone == phone


def test_phone_is_stripped():
    assert PhoneSendCodeRequest(phone=" +79991234567 ").phone == "+79991234567"


@pytest.mark.parametrize(
    "phone",
    [
        "79991234567",       # без +
        "+0991234567",       # ведущий ноль после +
        "+7 999 123 45 67",  # пробелы внутри
        "+7999123",          # слишком короткий
        "+799912345678901234",  # длиннее 15 цифр
        "not-a-phone",
    ],
)
def test_invalid_e164_rejected(phone):
    with pytest.raises(ValidationError):
        PhoneSendCodeRequest(phone=phone)


def test_verify_code_format():
    req = PhoneVerifyCodeRequest(phone="+79991234567", code="123456")
    assert req.code == "123456"
    with pytest.raises(ValidationError):
        PhoneVerifyCodeRequest(phone="+79991234567", code="12ab56")
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `python -m pytest tests/unit/test_phone_schemas.py -q`
Expected: FAIL — `ImportError: cannot import name 'PhoneSendCodeRequest'`.

- [ ] **Step 3: Реализовать схемы**

В `app/schemas/user.py` — импорты и общий валидатор:

```python
import re
from typing import Annotated

from pydantic import AfterValidator, Field

# E.164: "+", первая цифра 1–9, всего 8–15 цифр. Нормализацию
# (пробелы/скобки/дефисы) сознательно НЕ делаем — фронт шлёт
# канонический формат, бэкенд строг (один номер = одна запись).
_E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")


def _validate_e164(v: str) -> str:
    v = v.strip()
    if not _E164_RE.fullmatch(v):
        raise ValueError(
            "Номер должен быть в формате E.164, например +79991234567"
        )
    return v


E164Phone = Annotated[str, AfterValidator(_validate_e164)]
```

Схемы запросов (рядом с остальными auth-схемами):

```python
class PhoneSendCodeRequest(BaseModel):
    phone: E164Phone


class PhoneVerifyCodeRequest(BaseModel):
    phone: E164Phone
    # Только цифры; длина с запасом под настройку otp_code_length (4–8).
    code: str = Field(pattern=r"^\d{4,8}$")
```

Поправить схемы ответов под nullable-поля (телефонный пользователь без email):

```python
class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str | None  # None у пользователей, вошедших по телефону
    phone: str | None = None
    is_active: bool
    is_email_verified: bool
    roles: list[RoleResponse]
    created_at: datetime
```

И гибкую доставку refresh (cookie-режим, Task 9):

```python
class TokenResponse(BaseModel):
    access_token: str
    # None в cookie-режиме (AUTH_REFRESH_COOKIE=true): refresh уходит
    # в httpOnly-cookie и в теле не дублируется.
    refresh_token: str | None = None
    token_type: str


class RefreshRequest(BaseModel):
    # None разрешён для веб-клиентов: роутер возьмёт refresh из cookie.
    refresh_token: str | None = None
```

- [ ] **Step 4: Прогнать тесты**

Run: `python -m pytest tests/unit/test_phone_schemas.py tests/unit tests/services -q`
Expected: новые тесты PASS; существующие тоже (поля стали Optional с дефолтами — обратная совместимость).

- [ ] **Step 5: Commit**

```bash
git add app/schemas/user.py tests/unit/test_phone_schemas.py
git commit -m "feat(auth): phone OTP request schemas with E.164 validation"
```

---

### Task 5: SMS-слой — абстракция провайдера + DI

**Files:**
- Create: `app/services/sms.py`
- Test: `tests/services/test_sms_provider.py`

- [ ] **Step 1: Написать падающие тесты**

Create `tests/services/test_sms_provider.py`:

```python
"""SMS-слой: Mock-провайдер, выбор по настройкам, маппинг ошибок sms.ru."""

import logging

import httpx
import pytest

from app.config import settings
from app.services import sms as sms_module
from app.services.sms import (
    MockSMSProvider,
    SMSDeliveryError,
    SmsRuProvider,
    get_sms_provider,
)


async def test_mock_provider_logs_message(caplog):
    provider = MockSMSProvider()
    with caplog.at_level(logging.INFO, logger="app.services.sms"):
        await provider.send("+79991234567", "Ваш код входа: 123456")
    assert "+79991234567" in caplog.text
    assert "123456" in caplog.text


def test_get_sms_provider_defaults_to_mock(monkeypatch):
    monkeypatch.setattr(sms_module, "_provider", None)
    monkeypatch.setattr(settings, "sms_provider", "mock")
    assert isinstance(get_sms_provider(), MockSMSProvider)


def test_get_sms_provider_smsru_requires_key(monkeypatch):
    monkeypatch.setattr(sms_module, "_provider", None)
    monkeypatch.setattr(settings, "sms_provider", "smsru")
    monkeypatch.setattr(settings, "sms_api_key", None)
    with pytest.raises(RuntimeError):
        get_sms_provider()


async def test_smsru_provider_error_status_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"status": "ERROR", "status_code": 202}
        )

    provider = SmsRuProvider("key", transport=httpx.MockTransport(handler))
    with pytest.raises(SMSDeliveryError):
        await provider.send("+79991234567", "code")


async def test_smsru_provider_network_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    provider = SmsRuProvider("key", transport=httpx.MockTransport(handler))
    with pytest.raises(SMSDeliveryError):
        await provider.send("+79991234567", "code")
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `python -m pytest tests/services/test_sms_provider.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.sms'`.

- [ ] **Step 3: Реализовать слой**

Create `app/services/sms.py`:

```python
"""
Слой интеграции с SMS-провайдерами.

Бизнес-логика (otp_auth) зависит только от абстракции SMSProvider и
получает реализацию через Depends(get_sms_provider) — подмена провайдера
(dev-mock, sms.ru, другой оператор) не трогает сервисы и роутеры.
"""

import logging
from abc import ABC, abstractmethod

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class SMSDeliveryError(Exception):
    """Провайдер не смог отправить SMS (сеть, баланс, ошибка API)."""


class SMSProvider(ABC):
    @abstractmethod
    async def send(self, phone: str, message: str) -> None:
        """Отправить SMS. Бросает SMSDeliveryError при сбое."""


class MockSMSProvider(SMSProvider):
    """Dev-провайдер: пишет сообщение в лог вместо реальной отправки."""

    async def send(self, phone: str, message: str) -> None:
        logger.info("[MOCK SMS] to=%s text=%r", phone, message)


class SmsRuProvider(SMSProvider):
    """
    sms.ru как пример реального провайдера (HTTP API).

    transport прокидывается для тестов (httpx.MockTransport); в проде
    остаётся None — httpx использует обычную сеть.
    """

    _URL = "https://sms.ru/sms/send"

    def __init__(
        self,
        api_key: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._transport = transport

    async def send(self, phone: str, message: str) -> None:
        try:
            async with httpx.AsyncClient(
                timeout=10, transport=self._transport
            ) as http:
                resp = await http.post(
                    self._URL,
                    data={
                        "api_id": self._api_key,
                        "to": phone,
                        "msg": message,
                        "json": 1,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as e:
            # Текст ошибки не отдаём клиенту (роутер вернёт 502) —
            # детали только в лог.
            logger.error("sms.ru request failed: %s", e)
            raise SMSDeliveryError("sms.ru request failed") from e
        if data.get("status") != "OK":
            logger.error("sms.ru rejected: %s", data)
            raise SMSDeliveryError(
                f"sms.ru status_code={data.get('status_code')}"
            )


# Singleton: провайдер не хранит состояние запроса, создавать на каждый
# Depends незачем.
_provider: SMSProvider | None = None


def get_sms_provider() -> SMSProvider:
    """FastAPI-dependency: реализация по settings.sms_provider."""
    global _provider
    if _provider is None:
        if settings.sms_provider == "smsru":
            if not settings.sms_api_key:
                raise RuntimeError(
                    "SMS_API_KEY обязателен при SMS_PROVIDER=smsru"
                )
            _provider = SmsRuProvider(settings.sms_api_key)
        else:
            if not settings.debug:
                # Mock в проде = коды уходят только в лог, вход по
                # телефону фактически не работает. Громко предупреждаем.
                logger.warning(
                    "SMS_PROVIDER=mock при DEBUG=False — SMS не отправляются"
                )
            _provider = MockSMSProvider()
    return _provider
```

- [ ] **Step 4: Прогнать тесты**

Run: `python -m pytest tests/services/test_sms_provider.py -q`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add app/services/sms.py tests/services/test_sms_provider.py
git commit -m "feat(auth): SMS provider abstraction (mock + sms.ru) with DI"
```

---

### Task 6: services/auth.py — guard юзеров без пароля + общий issue_token_pair

**Files:**
- Modify: `app/services/auth.py:149-249, 273-296, 419-440`
- Test: `tests/services/test_auth_security.py` (добавить тесты)

- [ ] **Step 1: Написать падающий тест**

В `tests/services/test_auth_security.py` добавить:

```python
# ---------- phone-only users: нет пароля — нет парольного входа ----------


async def test_login_phone_only_user_rejected(monkeypatch):
    """Юзер без hashed_password (вход по телефону) не логинится паролем."""
    user = _fake_user()
    user.hashed_password = None
    monkeypatch.setattr(
        user_repo, "get_user_by_email", AsyncMock(return_value=user)
    )

    with pytest.raises(ValueError, match="invalid_credentials"):
        await auth_service.login_user(
            _fake_session(), "alice@example.com", "CorrectPass1"
        )


async def test_change_password_phone_only_user_403(monkeypatch):
    user = _fake_user()
    user.hashed_password = None

    with pytest.raises(HTTPException) as exc:
        await auth_service.change_password(
            _fake_session(),
            user,
            "anything",
            "NewPass123",
            ip=None,
            user_agent=None,
        )
    assert exc.value.status_code == 403
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `python -m pytest tests/services/test_auth_security.py -q`
Expected: новые тесты FAIL — `verify_password` падает с `TypeError` на `hashed=None` (или passlib-ошибка), а не контролируемые `ValueError`/403.

- [ ] **Step 3: Реализовать**

В `app/services/auth.py`:

3a. Новый публичный хелпер перед `login_user` (выносит дублированную выдачу токенов из `login_user` / `refresh_access_token`; нужен и `verify_otp_code` в Task 8):

```python
async def issue_token_pair(db: AsyncSession, user) -> TokenResponse:
    """
    Выдать пару access+refresh для уже аутентифицированного пользователя.
    Коммитит транзакцию. Вызывающий ОБЯЗАН проверить is_active до вызова.
    """
    roles = [r.role.value for r in user.roles]
    access = create_access_token(str(user.id), roles)

    raw_refresh = create_refresh_token_value()
    refresh_hash = hash_token(raw_refresh)
    expires_at = datetime.now(timezone.utc) + timedelta(
        days=settings.refresh_token_expire_days
    )
    await user_repo.create_refresh_token(db, user.id, refresh_hash, expires_at)
    await db.commit()

    return TokenResponse(
        access_token=access, refresh_token=raw_refresh, token_type="bearer"
    )
```

3b. В `login_user` — guard перед `verify_password` (комментарий про timing attack сохранить):

```python
    # Phone-OTP: у телефонного пользователя пароля нет — парольный вход
    # для него закрыт. dummy-верификация выравнивает время ответа.
    if not user.hashed_password:
        dummy_verify_password()
        security_logger.info("login_failed reason=no_password user_id=%s", user.id)
        raise ValueError("invalid_credentials")

    if not verify_password(password, user.hashed_password):
        ...
```

3c. Хвост `login_user` (от `roles = [...]` до `return TokenResponse(...)`) заменить на:

```python
    return await issue_token_pair(db, user)
```

3d. Аналогично хвост `refresh_access_token` (от `roles = [...]` до `return TokenResponse(...)`) заменить на `return await issue_token_pair(db, user)`.

3e. В `request_email_change` и `change_password` первый guard дополнить проверкой наличия пароля (телефонный пользователь без пароля получает тот же 403 — не раскрываем тип аккаунта):

```python
    if (
        not user.hashed_password
        or not current_password
        or not verify_password(current_password, user.hashed_password)
    ):
```

(в `change_password` — `if not user.hashed_password or not verify_password(current_password, user.hashed_password):`; следом стоящий `verify_password(new_password, ...)` уже защищён этим guard'ом).

- [ ] **Step 4: Прогнать тесты**

Run: `python -m pytest tests/services/test_auth_security.py -q`
Expected: все PASS, включая старые (рефакторинг хвостов не меняет поведение).

- [ ] **Step 5: Commit**

```bash
git add app/services/auth.py tests/services/test_auth_security.py
git commit -m "refactor(auth): shared issue_token_pair + passwordless user guards"
```

---

### Task 7: OTP-сервис — отправка кода

**Files:**
- Create: `app/services/otp_auth.py`
- Test: `tests/services/test_otp_auth.py`

- [ ] **Step 1: Написать падающие тесты**

Create `tests/services/test_otp_auth.py`:

```python
"""OTP-сервис: cooldown, суточный лимит, попытки, одноразовость кода.

Redis и репозиторий замоканы (паттерн test_auth_security.py) — тесты
проверяют логику и порядок Redis-команд, не сами хранилища.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.repositories import user as user_repo
from app.services import otp_auth
from app.utils.security import hash_token

PHONE = "+79991234567"


def _redis(**overrides):
    """AsyncMock Redis: по умолчанию — «чистое» состояние."""
    r = MagicMock()
    r.set = AsyncMock(return_value=True)       # SET NX EX прошёл
    r.get = AsyncMock(return_value=None)
    r.incr = AsyncMock(return_value=1)
    r.expire = AsyncMock()
    r.delete = AsyncMock()
    for name, value in overrides.items():
        setattr(r, name, value)
    return r


def _sms():
    sms = MagicMock()
    sms.send = AsyncMock()
    return sms


# ---------- send_otp_code ----------


async def test_send_stores_hash_and_sends_sms():
    redis, sms = _redis(), _sms()

    await otp_auth.send_otp_code(redis, sms, PHONE)

    sms.send.assert_awaited_once()
    sent_phone, message = sms.send.await_args.args
    assert sent_phone == PHONE
    # В Redis ушёл ХЕШ кода из SMS, с TTL из настроек.
    code = next(
        w for w in message.split() if w.isdigit()
    )
    stored = [
        c for c in redis.set.await_args_list
        if c.args[0] == f"otp:code:{PHONE}"
    ]
    assert stored[0].args[1] == hash_token(code)
    assert stored[0].kwargs["ex"] == settings.otp_code_ttl_seconds


async def test_send_cooldown_raises_rate_limited():
    # SET NX вернул None → SMS уже уходило < cooldown назад.
    redis, sms = _redis(set=AsyncMock(return_value=None)), _sms()

    with pytest.raises(otp_auth.OTPRateLimitedError):
        await otp_auth.send_otp_code(redis, sms, PHONE)
    sms.send.assert_not_called()


async def test_send_daily_limit_raises_rate_limited():
    redis, sms = _redis(), _sms()
    redis.incr = AsyncMock(return_value=settings.otp_daily_limit + 1)

    with pytest.raises(otp_auth.OTPRateLimitedError):
        await otp_auth.send_otp_code(redis, sms, PHONE)
    sms.send.assert_not_called()
```

(тесты `verify_otp_code` добавятся в Task 8 в этот же файл)

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `python -m pytest tests/services/test_otp_auth.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.otp_auth'`.

- [ ] **Step 3: Реализовать send_otp_code**

Create `app/services/otp_auth.py`:

```python
"""
Бизнес-логика входа по телефону с OTP-кодом.

Состояние OTP живёт в Redis (TTL делает коды самоистекающими):
  otp:cooldown:{phone} — маркер «SMS уже отправлено» (SET NX EX, атомарно)
  otp:code:{phone}     — SHA-256 кода (не сам код), TTL = otp_code_ttl_seconds
  otp:attempts:{phone} — счётчик попыток ввода (INCR атомарен)
  otp:daily:{phone}    — суточный счётчик отправок (анти SMS-pumping)
"""

import logging
import secrets

from redis.asyncio import Redis
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.repositories import user as user_repo
from app.schemas.user import TokenResponse
from app.services.auth import issue_token_pair
from app.services.sms import SMSProvider
from app.utils.security import hash_token

logger = logging.getLogger(__name__)
security_logger = logging.getLogger("app.security")


class OTPRateLimitedError(Exception):
    """Повторная отправка раньше cooldown / суточный потолок. → 429"""


class OTPExpiredError(Exception):
    """Кода нет: истёк, не запрашивался или сожжён попытками. → 401"""


class OTPInvalidError(Exception):
    """Код неверный, попытки ещё остались. → 400"""


class OTPUserBlockedError(Exception):
    """Код верный, но пользователь заблокирован (is_active=False). → 401"""


def _cooldown_key(phone: str) -> str:
    return f"otp:cooldown:{phone}"


def _code_key(phone: str) -> str:
    return f"otp:code:{phone}"


def _attempts_key(phone: str) -> str:
    return f"otp:attempts:{phone}"


def _daily_key(phone: str) -> str:
    return f"otp:daily:{phone}"


def _generate_code() -> str:
    # secrets (не random): криптографический RNG. Ведущие нули сохраняем
    # форматированием — код всегда фиксированной длины.
    n = settings.otp_code_length
    return f"{secrets.randbelow(10 ** n):0{n}d}"


async def send_otp_code(redis: Redis, sms: SMSProvider, phone: str) -> None:
    # 1. Cooldown: SET NX EX атомарен — из двух параллельных запросов
    #    SMS отправит ровно один.
    ok = await redis.set(
        _cooldown_key(phone),
        "1",
        nx=True,
        ex=settings.otp_send_cooldown_seconds,
    )
    if not ok:
        security_logger.info("otp_send_cooldown phone=%s", phone)
        raise OTPRateLimitedError

    # 2. Суточный потолок на номер. INCR атомарен; expire ставим только
    #    первому инкременту — окно скользит от первой отправки.
    daily = await redis.incr(_daily_key(phone))
    if daily == 1:
        await redis.expire(_daily_key(phone), 86400)
    if daily > settings.otp_daily_limit:
        security_logger.warning("otp_daily_limit phone=%s", phone)
        raise OTPRateLimitedError

    # 3. Новый код перезаписывает старый (валиден только последний),
    #    счётчик попыток обнуляется.
    code = _generate_code()
    await redis.set(
        _code_key(phone), hash_token(code), ex=settings.otp_code_ttl_seconds
    )
    await redis.delete(_attempts_key(phone))

    # 4. Отправка. Сбой провайдера пробрасывается (роутер → 502);
    #    cooldown при этом остаётся — клиент не должен долбить ретраями.
    await sms.send(phone, f"Ваш код входа: {code}")

    if settings.debug:
        # Dev-flow без SMS-шлюза: код в логе. В проде — никогда.
        logger.info("[DEV] OTP for %s: %s", phone, code)
    else:
        security_logger.info("otp_sent phone=%s", phone)
```

- [ ] **Step 4: Прогнать тесты**

Run: `python -m pytest tests/services/test_otp_auth.py -q`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add app/services/otp_auth.py tests/services/test_otp_auth.py
git commit -m "feat(auth): OTP send-code service (cooldown, daily cap, hashed code)"
```

---

### Task 8: OTP-сервис — проверка кода и выдача токенов

**Files:**
- Modify: `app/services/otp_auth.py`
- Test: `tests/services/test_otp_auth.py` (дополнить)

- [ ] **Step 1: Написать падающие тесты**

Добавить в `tests/services/test_otp_auth.py`:

```python
from uuid import uuid4

from app.services import auth as auth_service


CODE = "123456"


def _fake_user(*, is_active: bool = True):
    user = MagicMock()
    user.id = uuid4()
    user.is_active = is_active
    user.roles = []
    return user


def _db():
    db = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.flush = AsyncMock()
    return db


def _redis_with_code(attempts: int = 1):
    return _redis(
        get=AsyncMock(return_value=hash_token(CODE)),
        incr=AsyncMock(return_value=attempts),
    )


# ---------- verify_otp_code ----------


async def test_verify_no_code_raises_expired():
    redis = _redis(get=AsyncMock(return_value=None))

    with pytest.raises(otp_auth.OTPExpiredError):
        await otp_auth.verify_otp_code(_db(), redis, PHONE, CODE)


async def test_verify_wrong_code_raises_invalid_and_keeps_code():
    redis = _redis_with_code(attempts=1)

    with pytest.raises(otp_auth.OTPInvalidError):
        await otp_auth.verify_otp_code(_db(), redis, PHONE, "000000")
    redis.delete.assert_not_called()  # попытки остались — код жив


async def test_verify_third_wrong_attempt_burns_code():
    redis = _redis_with_code(attempts=settings.otp_max_attempts)

    with pytest.raises(otp_auth.OTPInvalidError):
        await otp_auth.verify_otp_code(_db(), redis, PHONE, "000000")
    redis.delete.assert_awaited_once_with(
        f"otp:code:{PHONE}", f"otp:attempts:{PHONE}"
    )


async def test_verify_over_limit_raises_expired():
    redis = _redis_with_code(attempts=settings.otp_max_attempts + 1)

    with pytest.raises(otp_auth.OTPExpiredError):
        await otp_auth.verify_otp_code(_db(), redis, PHONE, CODE)
    redis.delete.assert_awaited()  # код сожжён


async def test_verify_success_existing_user(monkeypatch):
    user = _fake_user()
    redis = _redis_with_code()
    monkeypatch.setattr(
        user_repo, "get_user_by_phone", AsyncMock(return_value=user)
    )
    issue = AsyncMock(return_value="TOKENS")
    monkeypatch.setattr(otp_auth, "issue_token_pair", issue)

    result = await otp_auth.verify_otp_code(_db(), redis, PHONE, CODE)

    assert result == "TOKENS"
    issue.assert_awaited_once()
    redis.delete.assert_awaited()  # код одноразовый


async def test_verify_success_creates_missing_user(monkeypatch):
    new_user = _fake_user()
    redis = _redis_with_code()
    monkeypatch.setattr(
        user_repo, "get_user_by_phone", AsyncMock(return_value=None)
    )
    create = AsyncMock(return_value=new_user)
    monkeypatch.setattr(user_repo, "create_user_by_phone", create)
    monkeypatch.setattr(
        otp_auth, "issue_token_pair", AsyncMock(return_value="TOKENS")
    )

    result = await otp_auth.verify_otp_code(_db(), redis, PHONE, CODE)

    assert result == "TOKENS"
    create.assert_awaited_once()


async def test_verify_blocked_user_rejected(monkeypatch):
    redis = _redis_with_code()
    monkeypatch.setattr(
        user_repo,
        "get_user_by_phone",
        AsyncMock(return_value=_fake_user(is_active=False)),
    )

    with pytest.raises(otp_auth.OTPUserBlockedError):
        await otp_auth.verify_otp_code(_db(), redis, PHONE, CODE)
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `python -m pytest tests/services/test_otp_auth.py -q`
Expected: новые тесты FAIL — `AttributeError: module ... has no attribute 'verify_otp_code'`.

- [ ] **Step 3: Реализовать verify_otp_code**

Добавить в `app/services/otp_auth.py`:

```python
async def verify_otp_code(
    db: AsyncSession, redis: Redis, phone: str, code: str
) -> TokenResponse:
    stored_hash = await redis.get(_code_key(phone))
    if stored_hash is None:
        security_logger.info("otp_verify_no_code phone=%s", phone)
        raise OTPExpiredError

    # Попытка регистрируется ДО сравнения: INCR атомарен, параллельные
    # запросы не получают «бесплатных» попыток.
    attempts = await redis.incr(_attempts_key(phone))
    if attempts == 1:
        # Счётчик живёт не дольше кода — иначе «висячие» попытки
        # блокировали бы СЛЕДУЮЩИЙ код (его счётчик чистит send).
        await redis.expire(_attempts_key(phone), settings.otp_code_ttl_seconds)
    if attempts > settings.otp_max_attempts:
        await redis.delete(_code_key(phone), _attempts_key(phone))
        security_logger.warning("otp_brute_force phone=%s", phone)
        raise OTPExpiredError

    # compare_digest: сравнение за константное время (timing attack).
    if not secrets.compare_digest(hash_token(code), stored_hash):
        if attempts >= settings.otp_max_attempts:
            # Последняя попытка истрачена — сжигаем код сразу.
            await redis.delete(_code_key(phone), _attempts_key(phone))
            security_logger.warning("otp_attempts_exhausted phone=%s", phone)
        else:
            security_logger.info(
                "otp_wrong_code phone=%s attempt=%s", phone, attempts
            )
        raise OTPInvalidError

    # Успех: код строго одноразовый.
    await redis.delete(_code_key(phone), _attempts_key(phone))

    # Find-or-create: подтверждённый номер = аутентифицированный
    # пользователь; отдельного шага «регистрация» нет.
    user = await user_repo.get_user_by_phone(db, phone)
    if user is None:
        try:
            user = await user_repo.create_user_by_phone(db, phone)
            security_logger.info("otp_user_created user_id=%s", user.id)
        except IntegrityError:
            # Race двух параллельных verify: UNIQUE(phone) пропустил
            # одного, второй читает созданного.
            await db.rollback()
            user = await user_repo.get_user_by_phone(db, phone)
            if user is None:
                raise OTPExpiredError

    if not user.is_active:
        security_logger.warning("otp_login_blocked user_id=%s", user.id)
        raise OTPUserBlockedError

    security_logger.info("otp_login_success user_id=%s", user.id)
    return await issue_token_pair(db, user)
```

- [ ] **Step 4: Прогнать тесты**

Run: `python -m pytest tests/services/test_otp_auth.py -q`
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add app/services/otp_auth.py tests/services/test_otp_auth.py
git commit -m "feat(auth): OTP verify-code service (attempts cap, find-or-create, JWT)"
```

---

### Task 9: Роутер — /auth/send-code, /auth/verify-code, cookie-режим

**Files:**
- Modify: `app/routers/auth.py`

- [ ] **Step 1: Эндпоинты OTP**

В `app/routers/auth.py` дополнить импорты:

```python
from fastapi import Response

from app.config import settings
from app.services.otp_auth import (
    OTPExpiredError,
    OTPInvalidError,
    OTPRateLimitedError,
    OTPUserBlockedError,
    send_otp_code,
    verify_otp_code,
)
from app.services.sms import SMSDeliveryError, SMSProvider, get_sms_provider
from app.schemas.user import PhoneSendCodeRequest, PhoneVerifyCodeRequest
```

Добавить хелпер cookie-режима и эндпоинты (после `/logout`):

```python
# Анти-enumeration: ответ одинаков для нового и существующего номера.
_SEND_CODE_RESPONSE = {"message": "Код отправлен"}


def _deliver_refresh(response: Response, tokens: TokenResponse) -> TokenResponse:
    """
    Гибкая доставка refresh-токена: по умолчанию — в теле (мобильный
    клиент), при AUTH_REFRESH_COOKIE=true — httpOnly-cookie для веба
    (XSS-устойчиво), в теле refresh_token=null.
    """
    if settings.auth_refresh_cookie and tokens.refresh_token:
        response.set_cookie(
            "refresh_token",
            tokens.refresh_token,
            httponly=True,
            secure=not settings.debug,
            samesite="strict",
            max_age=settings.refresh_token_expire_days * 86400,
            # Cookie уходит только на /auth/* (refresh, logout) —
            # минимизирует поверхность утечки.
            path="/auth",
        )
        tokens.refresh_token = None
    return tokens


@router.post(
    "/send-code",
    summary="Отправка OTP-кода на телефон",
    description=(
        "Принимает номер в E.164, отправляет SMS с одноразовым кодом "
        "(TTL 5 минут). Повторная отправка на тот же номер — не чаще "
        "раза в 60 секунд (429). Ответ одинаков для нового и "
        "существующего номера (анти-enumeration)."
    ),
)
async def send_code(
    request: Request,
    body: PhoneSendCodeRequest,
    redis: Redis = Depends(get_redis),
    sms: SMSProvider = Depends(get_sms_provider),
):
    # IP-лимит поверх per-phone cooldown'а: cooldown не мешает перебирать
    # РАЗНЫЕ номера с одного IP (SMS pumping). bug_247: fail_closed.
    await check_rate_limit(
        request, limit=5, window=60, redis=redis, fail_closed=True
    )
    try:
        await send_otp_code(redis, sms, body.phone)
    except OTPRateLimitedError:
        raise HTTPException(status_code=429, detail="too_many_requests")
    except SMSDeliveryError:
        # Детали провайдера наружу не отдаём; cooldown уже стоит.
        raise HTTPException(status_code=502, detail="sms_delivery_failed")
    return _SEND_CODE_RESPONSE


@router.post(
    "/verify-code",
    summary="Вход/регистрация по OTP-коду",
    description=(
        "Проверяет код из SMS (максимум 3 попытки, затем код сжигается). "
        "При первом входе создаёт пользователя по номеру. Возвращает "
        "access + refresh (refresh — в httpOnly-cookie при "
        "AUTH_REFRESH_COOKIE=true). 400 — неверный код, 401 — код "
        "истёк/исчерпан."
    ),
)
async def verify_code(
    request: Request,
    body: PhoneVerifyCodeRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> TokenResponse:
    await check_rate_limit(
        request, limit=10, window=60, redis=redis, fail_closed=True
    )
    try:
        tokens = await verify_otp_code(db, redis, body.phone, body.code)
    except OTPExpiredError:
        raise HTTPException(status_code=401, detail="code_expired")
    except OTPUserBlockedError:
        raise HTTPException(status_code=401, detail="user_blocked")
    except OTPInvalidError:
        raise HTTPException(status_code=400, detail="invalid_code")
    return _deliver_refresh(response, tokens)
```

- [ ] **Step 2: Cookie-fallback в /auth/refresh и /auth/logout**

В обоих эндпоинтах вместо прямого `body.refresh_token` — извлечение с fallback (веб-клиент в cookie-режиме тело не шлёт):

```python
def _extract_refresh(request: Request, body: RefreshRequest) -> str:
    raw = body.refresh_token or request.cookies.get("refresh_token")
    if not raw:
        raise HTTPException(status_code=401, detail="missing_refresh_token")
    return raw
```

В `refresh`: `return _deliver_refresh(response, await refresh_access_token(db, _extract_refresh(request, body)))` — добавить параметр `response: Response` в сигнатуру (rotation в cookie-режиме обновляет cookie).
В `logout`: `await logout_user(db, _extract_refresh(request, body))`, плюс в cookie-режиме почистить cookie:

```python
    if settings.auth_refresh_cookie:
        response.delete_cookie("refresh_token", path="/auth")
```

(тоже добавить `response: Response` в сигнатуру `logout`).

- [ ] **Step 3: Прогнать существующие тесты**

Run: `python -m pytest tests/services tests/unit -q`
Expected: PASS. Поведение по умолчанию (флаг выключен) не изменилось: refresh в теле, cookie не ставится.

- [ ] **Step 4: Commit**

```bash
git add app/routers/auth.py
git commit -m "feat(auth): /auth/send-code and /auth/verify-code endpoints + cookie mode"
```

---

### Task 10: Интеграционный тест полного флоу

**Files:**
- Test: `tests/integration/test_phone_auth_flow.py`

- [ ] **Step 1: Написать тесты**

Create `tests/integration/test_phone_auth_flow.py`:

```python
"""
Интеграция phone-OTP: HTTP-стек + PostgreSQL + Redis.

SMS перехватывается подменой get_sms_provider через dependency_overrides —
тест достаёт код из «отправленного» сообщения, как это сделал бы телефон.
"""

from __future__ import annotations

import random
import re

import pytest_asyncio

from app.main import app
from app.services.sms import SMSProvider, get_sms_provider


def _phone() -> str:
    return f"+7999{random.randint(1000000, 9999999)}"


class _CaptureSMS(SMSProvider):
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, phone: str, message: str) -> None:
        self.sent.append((phone, message))

    def last_code(self) -> str:
        match = re.search(r"\d{4,8}", self.sent[-1][1])
        assert match, f"в SMS нет кода: {self.sent[-1][1]!r}"
        return match.group()


@pytest_asyncio.fixture
async def sms_capture():
    provider = _CaptureSMS()
    app.dependency_overrides[get_sms_provider] = lambda: provider
    yield provider
    app.dependency_overrides.pop(get_sms_provider, None)


async def test_full_flow_creates_user_and_logs_in(client, sms_capture):
    phone = _phone()

    # 1. Отправка кода.
    r = await client.post("/auth/send-code", json={"phone": phone})
    assert r.status_code == 200, r.text
    code = sms_capture.last_code()

    # 2. Верный код → пара токенов, пользователь создан.
    r = await client.post(
        "/auth/verify-code", json={"phone": phone, "code": code}
    )
    assert r.status_code == 200, r.text
    tokens = r.json()
    assert tokens["access_token"] and tokens["refresh_token"]

    # 3. Access-токен работает; у телефонного юзера email пуст.
    r = await client.get(
        "/users/me",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["email"] is None

    # 4. Код одноразовый: повторный verify тем же кодом → 401.
    r = await client.post(
        "/auth/verify-code", json={"phone": phone, "code": code}
    )
    assert r.status_code == 401

    # 5. Refresh-токен принимается стандартным /auth/refresh.
    r = await client.post(
        "/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert r.status_code == 200, r.text


async def test_second_login_reuses_user(client, sms_capture):
    phone = _phone()
    await client.post("/auth/send-code", json={"phone": phone})
    code = sms_capture.last_code()
    r = await client.post(
        "/auth/verify-code", json={"phone": phone, "code": code}
    )
    user_id_1 = _user_id(r.json()["access_token"])

    # Cooldown в Redis мешает второму send — чистим ключ, как будто
    # прошла минута.
    # (фикстура test_redis доступна через client; проще новый номер не
    # брать, а удалить cooldown-ключ)


async def test_cooldown_returns_429(client, sms_capture):
    phone = _phone()
    r = await client.post("/auth/send-code", json={"phone": phone})
    assert r.status_code == 200
    r = await client.post("/auth/send-code", json={"phone": phone})
    assert r.status_code == 429


async def test_three_wrong_attempts_burn_code(client, sms_capture):
    phone = _phone()
    await client.post("/auth/send-code", json={"phone": phone})
    code = sms_capture.last_code()
    wrong = "000000" if code != "000000" else "111111"

    for _ in range(3):
        r = await client.post(
            "/auth/verify-code", json={"phone": phone, "code": wrong}
        )
        assert r.status_code == 400

    # Код сожжён — даже ВЕРНЫЙ код теперь даёт 401.
    r = await client.post(
        "/auth/verify-code", json={"phone": phone, "code": code}
    )
    assert r.status_code == 401


async def test_invalid_phone_format_422(client, sms_capture):
    r = await client.post("/auth/send-code", json={"phone": "89991234567"})
    assert r.status_code == 422
```

Тест `test_second_login_reuses_user` дописать так: принять фикстуру `test_redis` и удалить ключ cooldown перед вторым send:

```python
async def test_second_login_reuses_user(client, sms_capture, test_redis):
    phone = _phone()
    await client.post("/auth/send-code", json={"phone": phone})
    code = sms_capture.last_code()
    r1 = await client.post(
        "/auth/verify-code", json={"phone": phone, "code": code}
    )
    assert r1.status_code == 200

    # Снимаем cooldown (как будто прошла минута) и входим повторно.
    await test_redis.delete(f"otp:cooldown:{phone}")
    await client.post("/auth/send-code", json={"phone": phone})
    code2 = sms_capture.last_code()
    r2 = await client.post(
        "/auth/verify-code", json={"phone": phone, "code": code2}
    )
    assert r2.status_code == 200

    # Один и тот же пользователь (sub в JWT), а не дубликат.
    from jose import jwt

    sub1 = jwt.get_unverified_claims(r1.json()["access_token"])["sub"]
    sub2 = jwt.get_unverified_claims(r2.json()["access_token"])["sub"]
    assert sub1 == sub2
```

(заготовку `_user_id` из черновика выше убрать — финальная версия использует `jwt.get_unverified_claims`).

- [ ] **Step 2: Прогнать интеграцию**

Поднять инфраструктуру, если не запущена: `docker compose up -d postgres redis` (имена сервисов сверить с `docker-compose.yml`).

Run: `python -m pytest tests/integration/test_phone_auth_flow.py -q`
Expected: 5 passed (или skip, если PG/Redis не подняты — тогда поднять и повторить; «skipped» не считается успехом задачи).

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_phone_auth_flow.py
git commit -m "test(auth): integration coverage for phone OTP flow"
```

---

### Task 11: Финальная верификация

- [ ] **Step 1: Полный прогон**

Run: `python -m pytest -q`
Expected: все тесты PASS (интеграционные — при поднятых PG+Redis).

Run: `ruff check app tests`
Expected: без ошибок.

- [ ] **Step 2: Ручной smoke через Swagger**

Запустить API (`uvicorn app.main:app --reload`), в Swagger (`/docs`):
1. `POST /auth/send-code` с `{"phone": "+79991234567"}` → 200, код виден в логе (`[MOCK SMS]` / `[DEV] OTP`).
2. `POST /auth/verify-code` с этим кодом → 200, пара токенов.
3. Повторный send-code сразу → 429.

- [ ] **Step 3: Commit (если были правки по итогам ruff)**

```bash
git add -A
git commit -m "chore(auth): lint fixes for phone OTP feature"
```

---

## Self-review (выполнен при написании плана)

- **Покрытие ТЗ:** E.164-валидация — Task 4; rate limit 60 с — Task 7 (`SET NX EX`); TTL кода 5 мин — Task 7; счётчик 3 попыток со сжиганием — Task 8; find-or-create в PostgreSQL — Task 8; JWT access+refresh — Task 8 через `issue_token_pair`; абстракция SMS с DI — Task 5; слои роутер/схемы/сервис/репозиторий — структура файлов; httpOnly-cookie опция — Task 9; коды 400/429/401 — Tasks 7–9.
- **Отклонение от ТЗ:** префикс `/auth` вместо `/api/v1/auth` — зафиксировано в «Решениях» п.1, причина — единообразие существующего API.
- **Типы сквозные:** `issue_token_pair` (Task 6) используется в Task 8; `TokenResponse.refresh_token: str | None` (Task 4) согласован с `_deliver_refresh` (Task 9); `E164Phone` (Task 4) используется обеими схемами.
