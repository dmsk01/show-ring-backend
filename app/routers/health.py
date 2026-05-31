"""
Health-check (этап 14, production-readiness).

GET /health возвращает JSON со статусом каждой зависимости. Используется
load balancer'ом для readiness-probe и docker-compose healthcheck'ами.

Стратегия: проверяем PG, Redis, RabbitMQ, MinIO **параллельно** через
asyncio.gather — иначе медленный сервис тормозит весь чек. HTTP-статус
200 всегда — внутренние ошибки в полях, не на уровне HTTP. Это даёт
фронту/прокси наблюдаемость без alert-flood'а.

Если нужен жёсткий probe ("любой компонент down = 503") — добавим
отдельный /health/ready (TODO).
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
# Импортируем модуль, а не значение: init_redis() пере-присваивает
# app.redis.redis_client уже после импорта health.py. `from app.redis import
# redis_client` связал бы стейл-None, и /health всегда показывал бы redis down.
from app import redis as redis_state
from app.services.rabbit import rabbit_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/health", tags=["health"])


async def _check_db(db: AsyncSession) -> str:
    try:
        await db.execute(text("SELECT 1"))
        return "ok"
    except (SQLAlchemyError, OSError) as e:
        logger.warning("health: db error %s", e)
        return "down"


async def _check_redis() -> str:
    client = redis_state.redis_client
    if client is None:
        return "down"
    try:
        # type: ignore — redis-py stubs возвращают bool, но в async-режиме
        # это всегда awaitable Coroutine. См. также app/redis.py с тем же
        # обходом.
        await client.ping()  # type: ignore[misc]
        return "ok"
    except Exception as e:  # noqa: BLE001
        logger.warning("health: redis error %s", e)
        return "down"


async def _check_rabbit() -> str:
    # rabbit_service.connection — aio_pika RobustConnection или None.
    # is_closed=True означает "ещё не подключились" или "разорвано".
    conn = rabbit_service.connection
    if conn is None or conn.is_closed:
        return "down"
    return "ok"


async def _check_minio() -> str:
    """
    Лёгкий head_bucket: HEAD без тела, минимальная нагрузка на MinIO.
    Альтернативы (list_buckets, get_object) тяжелее.
    """
    try:
        from app.services.file_storage import _s3_client

        async with _s3_client() as s3:
            await s3.head_bucket(Bucket=settings.s3_bucket)
        return "ok"
    except Exception as e:  # noqa: BLE001
        logger.warning("health: minio error %s", e)
        return "down"


@router.get("/")
async def health_check(db: AsyncSession = Depends(get_db)):
    """
    Параллельные проверки через asyncio.gather. Каждая корутина
    самостоятельно ловит исключения и возвращает "down" — gather
    не падает, мы получаем полный отчёт за один round-trip.
    """
    db_s, redis_s, rabbit_s, minio_s = await asyncio.gather(
        _check_db(db),
        _check_redis(),
        _check_rabbit(),
        _check_minio(),
    )
    return {
        "status": "ok",
        "components": {
            "db": db_s,
            "redis": redis_s,
            "rabbitmq": rabbit_s,
            "minio": minio_s,
        },
    }


@router.get("/ready")
async def readiness_probe(db: AsyncSession = Depends(get_db)):
    """
    Жёсткий readiness-probe для load balancer'ов: 503 если ЛЮБОЙ
    критичный компонент down. Отдельно от /health, чтобы:
    - /health показывал детальное состояние (для дашборда),
    - /ready давал бинарное «можно/нельзя слать трафик» (для LB).

    Что считается критичным: PG (без БД API не работает). Redis/Rabbit/
    MinIO — некоторые эндпоинты деградируют без них, но базовая работа
    есть; их падение НЕ должно выводить инстанс из ротации.
    """
    db_s = await _check_db(db)
    if db_s != "ok":
        raise HTTPException(status_code=503, detail={"db": db_s})
    return {"status": "ready"}


# Дев-эндпоинт для тестирования ErrorHandler — доступен только в debug.
if settings.debug:
    @router.get("/test-error")
    async def test_error():
        raise ValueError("test unhandled exception")
