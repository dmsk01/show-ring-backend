# Этап 13: Тестирование

### Цель

Покрыть проект тестами: unit-тесты бизнес-логики, интеграционные тесты API, тесты правил РКФ.

### Что появляется в проекте

- Тестовая инфраструктура: фикстуры, тестовая БД, моки
- Unit-тесты: services, show_rules, repositories
- Интеграционные тесты: API endpoints через httpx
- Тесты правил РКФ: определение классов, присвоение титулов
- Тесты RabbitMQ: моки для publisher, проверка отправки сообщений
- pytest configuration, coverage report

### Новые зависимости

```
pytest
pytest-asyncio
httpx
pytest-cov
factory-boy  # опционально — фабрики тестовых данных
```

### Структура тестов

```
tests/
├── conftest.py                 # Фикстуры: test DB, client, mocks
├── factories.py                # Фабрики тестовых данных (User, Dog, Show)
├── unit/
│   ├── test_show_rules.py      # Правила РКФ: классы, титулы
│   ├── test_auth_service.py    # JWT, хеширование
│   └── test_ad_service.py      # Таргетинг баннеров
├── integration/
│   ├── test_auth.py            # POST /auth/register, /auth/login
│   ├── test_kennels.py         # CRUD питомников
│   ├── test_dogs.py            # CRUD собак, родословная
│   ├── test_shows.py           # Создание выставки, запись, результаты
│   ├── test_classifieds.py     # Объявления, поиск
│   └── test_documents.py       # Генерация (с моком RabbitMQ)
└── pytest.ini
```

### Ключевые тесты

**Правила РКФ (unit):**
```python
class TestShowRules:
    def test_determine_class_junior(self):
        """Собака 12 месяцев → класс юниоров"""

    def test_determine_class_veteran(self):
        """Собака 9 лет → класс ветеранов"""

    def test_cw_gets_cac_in_open_class(self):
        """CW в открытом классе → автоматически CAC"""

    def test_cw_no_cac_in_junior_class(self):
        """CW в классе юниоров → ЮСАС, но не CAC"""

    def test_best_of_breed_selection(self):
        """ЛК vs ЛС vs лучший юниор → BOB"""
```

**Интеграционные (API):**
```python
class TestShowsAPI:
    async def test_create_show(self, client, organizer_token):
        """Организатор создаёт выставку"""

    async def test_entry_validates_age(self, client, breeder_token):
        """Щенок 3 месяца — отказ в записи"""

    async def test_entry_auto_class(self, client, breeder_token):
        """Собака 14 месяцев → класс юниоров автоматически"""

    async def test_unauthorized_create_show(self, client, buyer_token):
        """Покупатель не может создать выставку → 403"""
```

### Файлы для создания

| Файл | Назначение |
|------|-----------|
| `tests/conftest.py` | Тестовая БД, AsyncClient, фикстуры пользователей |
| `tests/factories.py` | Фабрики данных |
| `tests/unit/test_show_rules.py` | Тесты правил РКФ |
| `tests/integration/test_auth.py` | Тесты авторизации |
| `tests/integration/test_shows.py` | Тесты выставок |
| `pytest.ini` | asyncio_mode = auto, testpaths |

### Ключевые концепции

- **Тестовая БД** — отдельная PostgreSQL database, создаётся/удаляется per session
- **dependency_overrides** — подмена RabbitMQ на mock в тестах
- **AsyncClient + ASGITransport** — HTTP-клиент без реального сервера
- **Фикстуры** — `@pytest.fixture` для переиспользования setup-логики
- **Coverage** — `pytest --cov=app --cov-report=term-missing`

### Как проверить

1. `pytest` — все тесты проходят
2. `pytest --cov=app` — покрытие > 70%
3. `pytest tests/unit/test_show_rules.py -v` — правила РКФ корректны
4. `pytest tests/integration/ -v` — API работает end-to-end
