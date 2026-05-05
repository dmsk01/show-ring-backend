# Этап 13: Хинты — Тестирование

Задачи идут в порядке реализации: сначала конфигурация и инфраструктура, потом unit-тесты, потом интеграционные.

---

## Задача 1: `pytest.ini` — конфигурация pytest

### 1. Что делать

Создать файл `pytest.ini` в корне проекта:

```ini
[pytest]
asyncio_mode = auto
testpaths = tests
```

Установить зависимости (добавить в `requirements.txt`):

```
pytest
pytest-asyncio
httpx
pytest-cov
factory-boy
```

Создать пустые `__init__.py`:
- `tests/__init__.py`
- `tests/unit/__init__.py`
- `tests/integration/__init__.py`

### 2. Как это работает

`asyncio_mode = auto` говорит `pytest-asyncio`: автоматически оборачивать каждый `async def test_...` в event loop. Без этого режима нужно ставить `@pytest.mark.asyncio` на каждый тест — многословно. `testpaths = tests` сообщает pytest где искать тесты (иначе он сканирует всё дерево проекта).

### 3. API / примеры

```ini
[pytest]
asyncio_mode = auto
testpaths = tests
```

Запуск тестов:

```bash
# Все тесты
pytest

# С покрытием
pytest --cov=app --cov-report=term-missing

# Только unit
pytest tests/unit/ -v

# Конкретный файл
pytest tests/unit/test_show_rules.py -v
```

### 4. Зачем это нужно

Без `asyncio_mode = auto` каждый async-тест нужно декорировать `@pytest.mark.asyncio` — при 50+ тестах это лишний шум. `testpaths` ускоряет сбор тестов и исключает случайные находки в `migrations/` или `docs/`.

### 5. Ключевые термины

- `asyncio_mode = auto` — автоматический async-режим; альтернатива ручному `@pytest.mark.asyncio`
- `testpaths` — директории, в которых pytest ищет тесты
- `pytest-asyncio` — плагин, добавляющий поддержку `async def` в тестах
- `pytest-cov` — плагин для измерения покрытия кода (использует `coverage.py`)

### 6. Как проверить

```bash
pytest --collect-only
# Должен вывести список найденных тест-файлов без ошибок
```

---

## Задача 2: `tests/conftest.py` — тестовая БД и фикстуры

### 1. Что делать

Создать файл `tests/conftest.py` с:

- `test_engine` / `TestSessionMaker` — подключение к отдельной тестовой БД `animaldemo_test`
- Fixture `create_test_db` (scope=`"session"`) — создаёт таблицы перед всеми тестами, удаляет после
- Fixture `db` — выдаёт `AsyncSession` для одного теста, откатывает транзакцию после
- Fixture `client` — `AsyncClient` с `ASGITransport`, подменяет `get_db` на тестовую сессию, мокает RabbitMQ
- Fixtures `buyer_token`, `breeder_token`, `organizer_token`, `admin_token` — JWT-токены тестовых пользователей

### 2. Как это работает

Ключевые три идеи:
1. **Отдельная БД** — `animaldemo_test` изолирует тесты от рабочих данных. `metadata.create_all` создаёт все таблицы разом (без Alembic — быстрее).
2. **`dependency_overrides`** — FastAPI позволяет в тестах заменить любой `Depends(...)` на другую функцию. Мы заменяем `get_db` на функцию, которая выдаёт тестовую сессию.
3. **`ASGITransport`** — `httpx` отправляет запросы напрямую в ASGI-приложение, минуя сеть. Не нужен запущенный сервер.

### 3. API / примеры

```python
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from unittest.mock import AsyncMock, patch

from app.main import app
from app.database import get_db
from app.models.base import Base
from app.utils.security import create_access_token

TEST_DB_URL = "postgresql+asyncpg://postgres:postgres@localhost:5432/animaldemo_test"

test_engine = create_async_engine(TEST_DB_URL, echo=False)
TestSessionMaker = async_sessionmaker(test_engine, expire_on_commit=False)


@pytest_asyncio.fixture(scope="session", autouse=True)
async def create_test_db():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def db():
    async with TestSessionMaker() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture
async def client(db: AsyncSession):
    async def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db

    with patch("app.messaging.rabbit.publish", new_callable=AsyncMock):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            yield ac

    app.dependency_overrides.clear()


@pytest.fixture
def buyer_token():
    return create_access_token({"sub": "100", "role": "buyer"})


@pytest.fixture
def breeder_token():
    return create_access_token({"sub": "101", "role": "breeder"})


@pytest.fixture
def organizer_token():
    return create_access_token({"sub": "102", "role": "organizer"})


@pytest.fixture
def admin_token():
    return create_access_token({"sub": "103", "role": "admin"})
```

### 4. Зачем это нужно

Без `dependency_overrides` интеграционный тест пишет в рабочую БД и требует запущенного сервера. `ASGITransport` делает тест быстрым и изолированным: FastAPI-приложение запускается в памяти. `session.rollback()` в конце каждого теста — чистая БД для следующего теста без удаления данных вручную.

### 5. Ключевые термины

- `dependency_overrides` — словарь FastAPI: `{original_dep: replacement_fn}`. Заменяет зависимость только на время теста
- `ASGITransport` — httpx-транспорт, который передаёт запросы прямо в ASGI-app
- `metadata.create_all` — создать все таблицы по ORM-метаданным; быстрая альтернатива `alembic upgrade head` для тестов
- `scope="session"` — fixture создаётся один раз на весь прогон тестов (не на каждый тест)
- `session.rollback()` — отменить все изменения сессии; гарантирует изолированность тестов

### 6. Как проверить

```bash
# Должен пройти без ошибок (даже если тестов ещё нет)
pytest tests/ --collect-only
```

---

## Задача 3: `tests/factories.py` — фабрики тестовых данных

### 1. Что делать

Создать файл `tests/factories.py` с фабриками для основных моделей: `UserFactory`, `KennelFactory`, `DogFactory`, `ShowFactory`.

Фабрики используют `factory-boy` с `SQLAlchemyModelFactory`. Каждая фабрика генерирует уникальные данные через `factory.Sequence` и `factory.Faker`.

### 2. Как это работает

`factory-boy` — библиотека для создания тестовых объектов. Объявляем класс с полями-дефолтами; при вызове `UserFactory.create(session=db)` он автоматически сохраняет строку в БД. `factory.Sequence` генерирует уникальные значения (email0, email1, ...) — нет конфликтов при запуске 100 тестов. `factory.Faker` использует библиотеку `Faker` для реалистичных данных.

### 3. API / примеры

```python
import factory
from factory.alchemy import SQLAlchemyModelFactory
from datetime import date, timedelta
from app.models.user import User
from app.models.dog import Dog
from app.models.show import Show
from app.utils.security import get_password_hash


class UserFactory(SQLAlchemyModelFactory):
    class Meta:
        model = User
        sqlalchemy_session_persistence = "commit"

    email = factory.Sequence(lambda n: f"user{n}@example.com")
    hashed_password = factory.LazyFunction(lambda: get_password_hash("testpass123"))
    role = "buyer"
    is_active = True


class DogFactory(SQLAlchemyModelFactory):
    class Meta:
        model = Dog
        sqlalchemy_session_persistence = "commit"

    name = factory.Faker("first_name")
    breed_id = 1
    # Возраст 2 года — попадает в открытый класс
    birthdate = factory.LazyFunction(lambda: date.today() - timedelta(days=730))
    owner_id = factory.SelfAttribute("..owner_id")


class ShowFactory(SQLAlchemyModelFactory):
    class Meta:
        model = Show
        sqlalchemy_session_persistence = "commit"

    title = factory.Sequence(lambda n: f"Выставка #{n}")
    status = "draft"
    organizer_id = 1
```

Использование в тесте:

```python
async def test_something(db: AsyncSession):
    UserFactory._meta.sqlalchemy_session = db
    user = UserFactory.create(role="breeder")
    assert user.id is not None
```

### 4. Зачем это нужно

Без фабрик каждый тест руками создаёт 5-10 объектов: `user = User(email=..., hashed_password=..., ...)`. При 50 тестах это сотни строк шаблонного кода. Фабрика — одна строка: `user = UserFactory.create(role="organizer")`. Изменение модели (новое обязательное поле) правится в одном месте.

### 5. Ключевые термины

- `SQLAlchemyModelFactory` — базовый класс factory-boy для SQLAlchemy; берёт `session` и сохраняет объект в БД
- `factory.Sequence(lambda n: ...)` — генератор уникальных значений; `n` = 0, 1, 2, ...
- `factory.Faker("field_name")` — данные от библиотеки Faker (имена, email, адреса)
- `factory.LazyFunction(fn)` — значение вычисляется при каждом создании объекта (не при объявлении класса)
- `sqlalchemy_session_persistence = "commit"` — автоматический `commit` после создания объекта

### 6. Как проверить

```bash
pytest tests/ -k "test_" --co -q
# В тестах, где используются фабрики, объекты должны создаваться без ошибок IntegrityError
```

---

## Задача 4: `tests/unit/test_show_rules.py` — правила РКФ

### 1. Что делать

Создать файл `tests/unit/test_show_rules.py` с unit-тестами для `app/services/show_rules.py`. Тесты должны покрывать:

- Определение класса по возрасту (`determine_class`)
- Правило: CW в открытом классе → автоматически CAC
- Правило: CW в классе юниоров → ЮСАС, но не CAC
- Алгоритм выбора BOB (лучший представитель породы)

Unit-тесты не используют БД или HTTP — только вызывают Python-функции напрямую.

### 2. Как это работает

`show_rules.py` — чистые функции без зависимостей (нет `db`, нет HTTP). Это делает их идеальными для unit-тестирования: вызвал с входными данными, проверил выходные. `pytest` собирает все функции `test_*` в классе `class TestShowRules` — группировка только для удобства, класс не должен наследоваться.

Возраст в РКФ считается на дату проведения выставки, а не сегодня:
- Щенок: 3–6 месяцев
- Юниор: 6–18 месяцев
- Промежуточный: 15–24 месяца
- Открытый: 15+ месяцев
- Ветеран: 8+ лет

### 3. API / примеры

```python
from datetime import date, timedelta
from app.services.show_rules import determine_class, assign_titles, select_best_of_breed


class TestDetermineClass:
    def test_puppy_class(self):
        birthdate = date.today() - timedelta(days=120)  # 4 месяца
        assert determine_class(birthdate, date.today()) == "puppy"

    def test_junior_class(self):
        birthdate = date.today() - timedelta(days=365)  # 12 месяцев
        assert determine_class(birthdate, date.today()) == "junior"

    def test_open_class(self):
        birthdate = date.today() - timedelta(days=730)  # 2 года
        assert determine_class(birthdate, date.today()) == "open"

    def test_veteran_class(self):
        birthdate = date.today() - timedelta(days=365 * 9)  # 9 лет
        assert determine_class(birthdate, date.today()) == "veteran"

    def test_too_young_raises(self):
        birthdate = date.today() - timedelta(days=60)  # 2 месяца
        with pytest.raises(ValueError, match="too young"):
            determine_class(birthdate, date.today())


class TestTitleAssignment:
    def test_cw_open_class_gets_cac(self):
        """CW в открытом классе автоматически получает CAC"""
        result = assign_titles(winner_class="open", is_class_winner=True)
        assert result["CAC"] is True

    def test_cw_junior_class_no_cac(self):
        """CW в классе юниоров получает ЮСАС, но не CAC"""
        result = assign_titles(winner_class="junior", is_class_winner=True)
        assert result.get("CAC") is False or "CAC" not in result
        assert result["JCAC"] is True

    def test_not_winner_no_titles(self):
        result = assign_titles(winner_class="open", is_class_winner=False)
        assert result.get("CAC") is not True
        assert result.get("JCAC") is not True


class TestBestOfBreed:
    def test_bob_from_best_male_female(self):
        """BOB выбирается из ЛК и ЛС"""
        best_male = {"id": 1, "grade": "excellent", "placement": 1}
        best_female = {"id": 2, "grade": "excellent", "placement": 1}
        bob = select_best_of_breed(best_male=best_male, best_female=best_female)
        assert bob["id"] in (1, 2)

    def test_bob_none_without_candidates(self):
        bob = select_best_of_breed(best_male=None, best_female=None)
        assert bob is None
```

### 4. Зачем это нужно

Правила РКФ — бизнес-логика, которая никогда не должна ломаться при рефакторинге. Unit-тест запускается за миллисекунды и не требует БД. Если кто-то случайно изменит порог юниора с 18 до 12 месяцев, тест упадёт мгновенно — до деплоя.

### 5. Ключевые термины

- Unit-тест — тест одной функции или класса без внешних зависимостей (БД, сеть)
- `pytest.raises(ExceptionClass)` — проверяет, что код бросает нужное исключение
- `timedelta(days=n)` — смещение даты; `date.today() - timedelta(days=365)` = год назад
- `class TestX` в pytest — просто группировка тестов; не нужен `__init__` и наследование
- `match="pattern"` в `pytest.raises` — проверяет что сообщение исключения содержит паттерн

### 6. Как проверить

```bash
pytest tests/unit/test_show_rules.py -v
# Все тесты должны пройти (зелёные)
# Вывод: PASSED для каждого test_*
```

---

## Задача 5: `tests/unit/test_auth_service.py` — JWT и хеширование

### 1. Что делать

Создать файл `tests/unit/test_auth_service.py` с тестами для `app/utils/security.py`:

- Хеширование пароля: `get_password_hash` + `verify_password`
- Создание JWT-токена: `create_access_token`
- Декодирование токена: `decode_access_token`
- Просроченный токен → исключение

### 2. Как это работает

Функции `get_password_hash` и `verify_password` — чистые (без IO), тестируются напрямую. `create_access_token` / `decode_access_token` тоже не требуют БД — работают только с JWT-библиотекой. Для теста просроченного токена используем `freeze_time` или передаём прошедшую дату в `expires_delta`.

### 3. API / примеры

```python
import pytest
from datetime import timedelta
from app.utils.security import (
    get_password_hash,
    verify_password,
    create_access_token,
    decode_access_token,
)


class TestPasswordHashing:
    def test_hash_is_not_plaintext(self):
        hashed = get_password_hash("mypassword")
        assert hashed != "mypassword"

    def test_verify_correct_password(self):
        hashed = get_password_hash("mypassword")
        assert verify_password("mypassword", hashed) is True

    def test_verify_wrong_password(self):
        hashed = get_password_hash("mypassword")
        assert verify_password("wrongpassword", hashed) is False

    def test_same_password_different_hashes(self):
        """bcrypt генерирует разные соли → разные хэши"""
        h1 = get_password_hash("mypassword")
        h2 = get_password_hash("mypassword")
        assert h1 != h2


class TestJWT:
    def test_create_and_decode_token(self):
        token = create_access_token({"sub": "42", "role": "breeder"})
        payload = decode_access_token(token)
        assert payload["sub"] == "42"
        assert payload["role"] == "breeder"

    def test_expired_token_raises(self):
        token = create_access_token(
            {"sub": "42"}, expires_delta=timedelta(seconds=-1)
        )
        with pytest.raises(Exception):  # JWTError или аналог
            decode_access_token(token)

    def test_tampered_token_raises(self):
        token = create_access_token({"sub": "42"})
        tampered = token[:-5] + "XXXXX"
        with pytest.raises(Exception):
            decode_access_token(tampered)
```

### 4. Зачем это нужно

Безопасность — критическая функция. Тест `test_verify_wrong_password` гарантирует, что `verify_password` не вернёт `True` по случайности. `test_tampered_token_raises` — что подделанный JWT не проходит. Без этих тестов уязвимость может жить незамеченной месяцами.

### 5. Ключевые термины

- `bcrypt` — алгоритм хеширования паролей; включает соль, сопротивляется brute-force
- `salt` — случайная строка, добавляемая перед хешированием; поэтому одинаковые пароли дают разные хэши
- `JWT` (JSON Web Token) — подписанный токен; подпись проверяется секретным ключом
- `expires_delta` — TTL токена; после истечения `decode` бросает исключение
- Тамперинг — изменение подписи токена; библиотека это детектирует

### 6. Как проверить

```bash
pytest tests/unit/test_auth_service.py -v
# Все 7 тестов — зелёные
```

---

## Задача 6: `tests/integration/test_auth.py` — тесты авторизации

### 1. Что делать

Создать файл `tests/integration/test_auth.py`. Тесты используют фикстуры `client` и `db` из `conftest.py`. Покрыть:

- `POST /auth/register` — успешная регистрация
- `POST /auth/register` — дублирующийся email → 409
- `POST /auth/login` — успешный вход → токен
- `POST /auth/login` — неверный пароль → 401
- `GET /auth/me` — с токеном → данные пользователя
- `GET /auth/me` — без токена → 401

### 2. Как это работает

Интеграционный тест проходит полный HTTP-цикл: `client.post(url, json=data)` → FastAPI роутер → сервис → репозиторий → тестовая БД. Проверяем `response.status_code` и `response.json()`. `db.rollback()` в конце теста (из фикстуры `db`) откатывает все записи — следующий тест видит чистую БД.

### 3. API / примеры

```python
import pytest


class TestRegister:
    async def test_register_success(self, client):
        response = await client.post("/auth/register", json={
            "email": "newuser@example.com",
            "password": "SecurePass123",
        })
        assert response.status_code == 201
        data = response.json()
        assert data["email"] == "newuser@example.com"
        assert "hashed_password" not in data  # пароль не утекает в ответ

    async def test_register_duplicate_email(self, client):
        payload = {"email": "dup@example.com", "password": "SecurePass123"}
        await client.post("/auth/register", json=payload)
        response = await client.post("/auth/register", json=payload)
        assert response.status_code == 409

    async def test_register_invalid_email(self, client):
        response = await client.post("/auth/register", json={
            "email": "not-an-email",
            "password": "SecurePass123",
        })
        assert response.status_code == 422


class TestLogin:
    async def test_login_success(self, client):
        await client.post("/auth/register", json={
            "email": "login@example.com",
            "password": "SecurePass123",
        })
        response = await client.post("/auth/login", json={
            "email": "login@example.com",
            "password": "SecurePass123",
        })
        assert response.status_code == 200
        assert "access_token" in response.json()

    async def test_login_wrong_password(self, client):
        await client.post("/auth/register", json={
            "email": "user@example.com",
            "password": "SecurePass123",
        })
        response = await client.post("/auth/login", json={
            "email": "user@example.com",
            "password": "WrongPassword",
        })
        assert response.status_code == 401

    async def test_login_unknown_email(self, client):
        response = await client.post("/auth/login", json={
            "email": "ghost@example.com",
            "password": "Whatever",
        })
        assert response.status_code == 401


class TestMe:
    async def test_me_with_token(self, client, buyer_token):
        response = await client.get(
            "/auth/me",
            headers={"Authorization": f"Bearer {buyer_token}"},
        )
        assert response.status_code == 200

    async def test_me_without_token(self, client):
        response = await client.get("/auth/me")
        assert response.status_code == 401
```

### 4. Зачем это нужно

Интеграционные тесты ловят баги, которые unit-тесты пропустят: неправильный SQL-запрос, забытый `await db.commit()`, опечатка в роутере. `test_register_duplicate_email` гарантирует, что уникальный индекс email работает и обрабатывается корректно (а не падает с `IntegrityError 500`).

### 5. Ключевые термины

- `response.status_code` — HTTP-код ответа: 200 OK, 201 Created, 401 Unauthorized, 409 Conflict, 422 Unprocessable
- `response.json()` — тело ответа как словарь Python
- `headers={"Authorization": "Bearer <token>"}` — HTTP-заголовок для JWT-авторизации
- `422 Unprocessable Entity` — FastAPI возвращает при ошибке валидации Pydantic
- `409 Conflict` — стандартный код для «ресурс уже существует» (дублирующийся email)

### 6. Как проверить

```bash
pytest tests/integration/test_auth.py -v
# 8 тестов — все зелёные
```

---

## Задача 7: `tests/integration/test_shows.py` — тесты выставок

### 1. Что делать

Создать файл `tests/integration/test_shows.py`. Покрыть основные сценарии:

- Организатор создаёт выставку → 201
- Покупатель не может создать выставку → 403
- Без токена не может создать выставку → 401
- Запись собаки на выставку → 201
- Щенок 3 месяца — отказ в записи → 400/422
- Собака 14 месяцев → автоматически юниор

### 2. Как это работает

Тесты с несколькими ролями проверяют RBAC (role-based access control): один эндпоинт, три запроса с разными токенами — три разных ответа. Фабрики (`DogFactory`, `ShowFactory`) создают данные прямо в тестовой БД через сессию `db`. RabbitMQ при записи на выставку замокирован в фикстуре `client` — тест не требует запущенного брокера.

### 3. API / примеры

```python
import pytest
from datetime import date, timedelta
from tests.factories import UserFactory, DogFactory, ShowFactory


class TestCreateShow:
    async def test_organizer_can_create_show(self, client, organizer_token):
        response = await client.post(
            "/shows",
            json={"title": "Весенняя выставка", "show_date": "2026-06-01"},
            headers={"Authorization": f"Bearer {organizer_token}"},
        )
        assert response.status_code == 201
        assert response.json()["title"] == "Весенняя выставка"

    async def test_buyer_cannot_create_show(self, client, buyer_token):
        response = await client.post(
            "/shows",
            json={"title": "Запрещённая выставка", "show_date": "2026-06-01"},
            headers={"Authorization": f"Bearer {buyer_token}"},
        )
        assert response.status_code == 403

    async def test_unauthenticated_cannot_create_show(self, client):
        response = await client.post(
            "/shows",
            json={"title": "Без токена", "show_date": "2026-06-01"},
        )
        assert response.status_code == 401


class TestShowEntry:
    async def test_entry_too_young_rejected(self, client, db, breeder_token):
        DogFactory._meta.sqlalchemy_session = db
        puppy = DogFactory.create(
            birthdate=date.today() - timedelta(days=60),  # 2 месяца
            owner_id=101,  # breeder_token user_id
        )
        ShowFactory._meta.sqlalchemy_session = db
        show = ShowFactory.create(status="open")

        response = await client.post(
            f"/shows/{show.id}/entries",
            json={"dog_id": puppy.id},
            headers={"Authorization": f"Bearer {breeder_token}"},
        )
        assert response.status_code in (400, 422)
        assert "too young" in response.json().get("detail", "").lower()

    async def test_entry_junior_class_auto_assigned(self, client, db, breeder_token):
        DogFactory._meta.sqlalchemy_session = db
        junior_dog = DogFactory.create(
            birthdate=date.today() - timedelta(days=420),  # 14 месяцев
            owner_id=101,
        )
        ShowFactory._meta.sqlalchemy_session = db
        show = ShowFactory.create(status="open")

        response = await client.post(
            f"/shows/{show.id}/entries",
            json={"dog_id": junior_dog.id},
            headers={"Authorization": f"Bearer {breeder_token}"},
        )
        assert response.status_code == 201
        assert response.json()["show_class"] == "junior"

    async def test_entry_success(self, client, db, breeder_token):
        DogFactory._meta.sqlalchemy_session = db
        dog = DogFactory.create(
            birthdate=date.today() - timedelta(days=730),  # 2 года
            owner_id=101,
        )
        ShowFactory._meta.sqlalchemy_session = db
        show = ShowFactory.create(status="open")

        response = await client.post(
            f"/shows/{show.id}/entries",
            json={"dog_id": dog.id},
            headers={"Authorization": f"Bearer {breeder_token}"},
        )
        assert response.status_code == 201
```

### 4. Зачем это нужно

`test_buyer_cannot_create_show` — гарантирует, что RBAC не сломался после рефакторинга. `test_entry_too_young_rejected` — тест бизнес-правила РКФ через HTTP: проверяет не только логику `show_rules.py`, но и то, что роутер правильно возвращает ошибку клиенту. Интеграционные тесты ловят регрессии на стыке слоёв.

### 5. Ключевые термины

- Регрессия — новый баг в старой функциональности; интеграционные тесты её детектируют
- RBAC (Role-Based Access Control) — управление доступом по роли; проверяется тестами с разными токенами
- `status_code in (400, 422)` — иногда валидный диапазон значений; 400 = бизнес-ошибка, 422 = ошибка схемы
- `Factory._meta.sqlalchemy_session = db` — привязать фабрику к тестовой сессии перед использованием
- Мок (mock) — заглушка; `AsyncMock` делает вид что функция отработала без реального вызова

### 6. Как проверить

```bash
pytest tests/integration/test_shows.py -v

# Полный прогон с покрытием:
pytest --cov=app --cov-report=term-missing
# Coverage должен быть > 70%
```
