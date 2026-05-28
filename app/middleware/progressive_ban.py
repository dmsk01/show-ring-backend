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


# ИСПРАВЛЕНО (bug_246 audit 2026-05-28): атомарная проверка-и-запись.
# Раньше zcard и zadd шли двумя отдельными командами Redis (даже в
# pipeline они НЕ атомарны: pipeline в redis-py async — это просто
# сгруппированная отправка по сети, между нашими командами может
# вклиниться другой клиент). Сценарий race: лимит=5, в set'е 4 запроса,
# два параллельных запроса оба видят count=4 < 5, оба zadd'ят → в set'е
# 6 запросов, лимит пробит. Lua выполняется в Redis атомарно — никакая
# другая команда не вклинится между шагами.
_RATE_LIMIT_SCRIPT = """
local rate_key = KEYS[1]
local ban_key = KEYS[2]
local violations_key = KEYS[3]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])

-- 1. Активный бан?
local ban_ttl = redis.call('ttl', ban_key)
if ban_ttl > 0 then
    return {1, ban_ttl}
end

-- 2. Sliding window cleanup + count
redis.call('zremrangebyscore', rate_key, 0, now - window)
local count = redis.call('zcard', rate_key)

-- 3. Превышение → новый бан экспоненциально.
if count >= limit then
    local v = redis.call('incr', violations_key)
    redis.call('expire', violations_key, 3600)
    local ban_seconds = math.min(2 ^ v, 3600)
    redis.call('setex', ban_key, ban_seconds, '1')
    return {1, math.floor(ban_seconds)}
end

-- 4. Запись текущего запроса в окно.
redis.call('zadd', rate_key, now, tostring(now))
redis.call('expire', rate_key, window)
return {0, 0}
"""


async def check_rate_limit(
    request: Request,
    limit: int,
    window: int,  # окно в секундах (60 = последняя минута)
    redis: Redis,
    *,
    fail_closed: bool = False,
) -> None:
    """
    Проверить rate limit для IP + endpoint.
    Поднимает HTTPException(429) с заголовком Retry-After при превышении.

    ИСПРАВЛЕНО (bug_247 audit 2026-05-28): добавлен per-call параметр
    `fail_closed`. Поведение при недоступности Redis:

    - fail_closed=False (default) — fail-open: логируем warning,
      пропускаем запрос. Для дешёвых публичных эндпоинтов (search,
      справочники) важнее доступность сайта, чем точная защита от
      накрутки на час сбоя Redis.

    - fail_closed=True — fail-closed: на любую ошибку Redis отдаём
      HTTPException(503). Используется для критичных операций (auth:
      login/register/refresh/verify-email/logout/password-reset), где
      «беззвучно отключившийся rate-limit» = открытое окно для
      credential stuffing'а и spam-регистраций. Атакующий, положивший
      Redis, одновременно бы получал unlimited попытки логина — теперь
      получит 503, что хуже для UX, но безопаснее для аккаунтов.

    Решение варианта B (per-call вместо глобального флага): глобальный
    `rate_limit_fail_closed=True` мог положить ВЕСЬ сайт при падении
    Redis — каскадный отказ. Per-call даёт точечную защиту критичных
    endpoint'ов, сохраняя доступность остальных.
    """
    ip = request.client.host if request.client else "unknown"
    endpoint = request.scope.get("path", request.url.path)

    rate_key = f"rate:{ip}:{endpoint}"  # sorted set запросов
    ban_key = f"ban:{ip}:{endpoint}"  # ключ активного бана
    violations_key = f"violations:{ip}:{endpoint}"  # счётчик нарушений

    try:
        now = time.time()
        # eval(script, numkeys, *keys_and_args). numkeys=3, дальше идут
        # 3 ключа, потом ARGV.
        result = await redis.eval(
            _RATE_LIMIT_SCRIPT,
            3,
            rate_key,
            ban_key,
            violations_key,
            now,
            window,
            limit,
        )
        # Lua возвращает массив [banned, retry_after]
        banned, retry_after = int(result[0]), int(result[1])
        if banned:
            raise HTTPException(
                status_code=429,
                detail="Too many requests",
                headers={"Retry-After": str(retry_after)},
            )
    except HTTPException:
        raise
    except Exception as e:
        # bug_247: на критичных эндпоинтах закрываем доступ, чтобы
        # отказ Redis не превращался в открытое окно атаки. Сообщение
        # клиенту нейтральное (не светим, что именно Redis лежит) +
        # Retry-After=60 — намёк, что это transient, можно ретраить.
        logger.warning(
            "Rate limit check failed for %s %s: %s (fail_closed=%s)",
            ip, endpoint, e, fail_closed,
        )
        if fail_closed:
            raise HTTPException(
                status_code=503,
                detail="Rate limit subsystem unavailable",
                headers={"Retry-After": "60"},
            )
