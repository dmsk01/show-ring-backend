import logging

from fastapi import HTTPException
from redis import RedisError
from redis.asyncio import Redis

from app.config import settings

logger = logging.getLogger(__name__)

redis_client: Redis | None = None


async def init_redis() -> None:
    """
    Создать клиент Redis. Клиент создаётся ВСЕГДА, даже если Redis сейчас
    недоступен: Redis.from_url не подключается сразу, а пул redis-py сам
    переподключится, когда Redis вернётся.

    ИСПРАВЛЕНО (ревью 2026-10-06, BE-02): раньше при неудачном ping клиент
    обнулялся до рестарта процесса — idempotency, дедуп рекламы, pub/sub и
    все cron-задачи (scheduler-lock требует клиента) молча отключались, а
    публичные ручки с Depends(get_redis) отдавали 503 вместо fail-open.
    Теперь сбой Redis — это ошибка конкретной операции, которую каждый
    потребитель обрабатывает по своей политике (fail-open/fail-closed).
    """
    global redis_client

    redis_client = Redis.from_url(
        settings.redis_url,
        decode_responses=True,
    )
    try:
        await redis_client.ping()  # type: ignore[misc]
        logger.info("Redis connected")
    except RedisError as e:
        logger.error("Redis unavailable at startup (will reconnect): %s", e)


async def close_redis() -> None:
    global redis_client
    if redis_client:
        await redis_client.aclose()
        redis_client = None


async def get_redis() -> Redis:
    if redis_client is None:
        raise HTTPException(503, "Redis недоступен")
    return redis_client
