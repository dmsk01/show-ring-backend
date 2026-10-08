"""
Капча ALTCHA: выдача задач виджету (app/services/captcha.py).
"""

from fastapi import APIRouter, Depends, Request
from redis.asyncio import Redis

from app.middleware.progressive_ban import check_rate_limit
from app.redis import get_redis
from app.services.captcha import new_challenge

router = APIRouter(prefix="/captcha", tags=["captcha"])


@router.get(
    "/challenge",
    summary="Задача капчи",
    description=(
        "Подписанная proof-of-work задача для виджета ALTCHA (формат v3). "
        "Решение отправляется полем `captcha` в /auth/send-code и (после "
        "нескольких неудачных попыток) в /auth/login."
    ),
)
async def get_challenge(request: Request, redis: Redis = Depends(get_redis)):
    # Выдача задачи дешёвая, но без лимита её можно использовать для
    # нагрузки: 60 в минуту с IP с запасом покрывают повторы виджета.
    await check_rate_limit(request, limit=60, window=60, redis=redis)
    return new_challenge()
