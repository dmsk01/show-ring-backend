from fastapi import HTTPException
import asyncio
from redis import RedisError
from app.config import settings
from redis.asyncio import Redis

redis_client: Redis | None = None


async def init_redis() -> None:
    global redis_client

    try:
        redis_client = Redis.from_url(
            settings.redis_url,
            decode_responses=True,
        )

        await redis_client.ping()  # type: ignore[misc]

        print("Redis connected")

    except RedisError as e:
        redis_client = None
        print(f"Redis connection failed: {e}")


async def close_redis() -> None:
    global redis_client
    if redis_client:
        await redis_client.aclose()
        redis_client = None


async def get_redis() -> Redis:
    if redis_client is None:
        raise HTTPException(503, "Redis недоступен")
    return redis_client
