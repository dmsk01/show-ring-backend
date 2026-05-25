import logging

from fastapi import HTTPException
from redis import RedisError
from redis.asyncio import Redis

from app.config import settings

logger = logging.getLogger(__name__)

redis_client: Redis | None = None


async def init_redis() -> None:
    global redis_client

    try:
        redis_client = Redis.from_url(
            settings.redis_url,
            decode_responses=True,
        )

        await redis_client.ping()  # type: ignore[misc]

        logger.info("Redis connected")

    except RedisError as e:
        redis_client = None
        # ИСПРАВЛЕНО: print → logging.warning, чтобы событие попало в лог-агрегатор.
        logger.warning("Redis connection failed: %s", e)


async def close_redis() -> None:
    global redis_client
    if redis_client:
        await redis_client.aclose()
        redis_client = None


async def get_redis() -> Redis:
    if redis_client is None:
        raise HTTPException(503, "Redis недоступен")
    return redis_client
