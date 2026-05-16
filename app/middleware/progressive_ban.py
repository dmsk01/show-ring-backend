# Алгоритм: sliding window + exponential ban
# 1. Запросы хранятся в sorted set (ключ = timestamp, score = timestamp)
# 2. При каждом запросе удаляем устаревшие записи и считаем оставшиеся
# 3. Если превышен лимит → инкрементируем счётчик нарушений → бан на 2^N секунд
# 4. Максимальный бан: 3600 секунд (1 час)
# 5. Счётчик нарушений сбрасывается через 1 час без нарушений
#
# Использование в роутере:
#   await check_rate_limit(request, "/auth/login", limit=5, window=60, redis=redis)

import time
import logging

from fastapi import Request, HTTPException
from redis.asyncio import Redis

logger = logging.getLogger(__name__)


async def check_rate_limit(
    request: Request,
    limit: int,
    window: int,  # окно в секундах (60 = последняя минута)
    redis: Redis,
) -> None:
    """
    Проверить rate limit для IP + endpoint.
    Поднимает HTTPException(429) с заголовком Retry-After при превышении.
    """
    ip = request.client.host if request.client else "unknown"
    endpoint = request.scope.get("path", request.url.path)

    rate_key = f"rate:{ip}:{endpoint}"  # sorted set запросов
    ban_key = f"ban:{ip}:{endpoint}"  # ключ активного бана
    violations_key = f"violations:{ip}:{endpoint}"  # счётчик нарушений

    try:
        # Шаг 1: проверить активный бан
        # ttl возвращает секунды до истечения, -2 если ключ не существует
        ban_ttl = await redis.ttl(ban_key)
        if ban_ttl > 0:
            raise HTTPException(
                status_code=429,
                detail="Too many requests",
                headers={"Retry-After": str(ban_ttl)},
            )

        # Шаг 2: sliding window
        now = time.time()
        pipe = redis.pipeline()
        # Удалить запросы старше окна (0 до now-window)
        pipe.zremrangebyscore(rate_key, 0, now - window)
        # Сколько запросов в текущем окне
        pipe.zcard(rate_key)
        _, count = await pipe.execute()

        if count >= limit:
            # Шаг 3: зафиксировать нарушение и установить экспоненциальный бан
            # incr создаёт ключ с 0 если не существует, инкрементирует и возвращает новое значение
            violations = await redis.incr(violations_key)
            # Сбрасывать счётчик через 1 час без нарушений
            await redis.expire(violations_key, 3600)

            ban_seconds = min(2**violations, 3600)  # 2, 4, 8, ..., 3600 (1 час макс)
            await redis.setex(ban_key, ban_seconds, "1")

            raise HTTPException(
                status_code=429,
                detail="Too many requests",
                headers={"Retry-After": str(ban_seconds)},
            )

        # Шаг 4: записать текущий запрос в sliding window
        # zadd: ключ + {member: score}, используем timestamp как member и score
        pipe = redis.pipeline()
        pipe.zadd(rate_key, {str(now): now})
        pipe.expire(rate_key, window)  # TTL = длина окна (автоочистка)
        await pipe.execute()
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("Rate limit check failed for %s %s: %s", ip, endpoint, e)
