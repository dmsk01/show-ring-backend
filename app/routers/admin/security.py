"""
Метрики безопасности для админа (план защиты 2026-10-05, этап 3).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from redis.asyncio import Redis

from app.dependencies import require_any_role
from app.redis import get_redis
from app.services import security_metrics

router = APIRouter(
    prefix="/admin/security",
    tags=["admin"],
    dependencies=[Depends(require_any_role("admin"))],
)


@router.get(
    "/metrics",
    summary="Метрики безопасности",
    description=(
        "Счётчики за 10 минут, час и сутки: ответы 429, ошибки 5xx, "
        "медленные запросы, отправленные SMS и верно введённые коды, "
        "отклонённые капчи, неудачные входы, блокировки аккаунтов."
    ),
)
async def get_security_metrics(redis: Redis = Depends(get_redis)):
    return {"windows": await security_metrics.summary(redis)}
