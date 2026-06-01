"""
Харнесс интеграционных тестов: реальный ASGI-стек FastAPI через httpx,
поверх настоящего PostgreSQL + Redis.

Изоляция БД: каждый тест работает внутри одной внешней транзакции, которая
в конце откатывается (rollback). Сессия приложения подключается к тому же
соединению с join_transaction_mode="create_savepoint" — commit'ы внутри
хендлеров становятся RELEASE SAVEPOINT и НЕ завершают внешнюю транзакцию.
Итог: после теста БД чиста, тесты не зависят друг от друга и не засоряют
dev-данные.

Redis: отдельная логическая БД (15), flushdb до и после теста — rate-limit
и idempotency не протекают между тестами и не трогают dev-кэш (db 0).

Если PostgreSQL или Redis недоступны — тесты пропускаются (skip), а не
падают: харнесс не должен «краснеть» там, где просто не поднята инфра.
"""

from __future__ import annotations

import re

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.database import get_db
from app.main import app
from app.redis import get_redis

# Отдельный движок для тестов с NullPool: pytest-asyncio запускает каждый
# тест в НОВОМ event loop, а пул asyncpg-соединений привязан к loop'у, на
# котором соединение открылось. Шаренный prod-движок (QueuePool) отдавал
# бы соединения от мёртвого loop'а → ошибки. NullPool не кэширует
# соединения: каждый connect() открывает новое на текущем loop'е и
# закрывает после — кросс-loop проблемы нет.
test_engine = create_async_engine(settings.database_url, poolclass=NullPool)


def _test_redis_url() -> str:
    """URL Redis с логической БД 15 (изолированно от dev db 0)."""
    url = settings.redis_url
    if re.search(r"/\d+$", url):
        return re.sub(r"/\d+$", "/15", url)
    return url.rstrip("/") + "/15"


@pytest_asyncio.fixture
async def test_redis():
    client = Redis.from_url(_test_redis_url(), decode_responses=True)
    try:
        await client.ping()
    except Exception:
        pytest.skip("Redis недоступен — интеграционные тесты требуют Redis")
    await client.flushdb()
    yield client
    try:
        await client.flushdb()
    finally:
        await client.aclose()


@pytest_asyncio.fixture
async def db_session():
    """AsyncSession в внешней транзакции с rollback в конце (изоляция)."""
    try:
        conn = await test_engine.connect()
    except Exception:
        pytest.skip("PostgreSQL недоступен — интеграционные тесты требуют БД")
    trans = await conn.begin()
    session = AsyncSession(
        bind=conn,
        expire_on_commit=False,
        # commit() приложения → RELEASE SAVEPOINT, внешняя транзакция жива.
        join_transaction_mode="create_savepoint",
    )
    try:
        yield session
    finally:
        await session.close()
        await trans.rollback()
        await conn.close()


@pytest_asyncio.fixture
async def client(db_session: AsyncSession, test_redis: Redis):
    """httpx-клиент поверх app с подменёнными get_db / get_redis."""

    async def _get_db_override():
        # Тот же session, что и фикстура db_session — тест видит, что
        # пишет хендлер, и наоборот, в рамках одной транзакции.
        yield db_session

    async def _get_redis_override():
        return test_redis

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_redis] = _get_redis_override
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as ac:
            yield ac
    finally:
        app.dependency_overrides.clear()
