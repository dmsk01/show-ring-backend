from fastapi import APIRouter, Depends, HTTPException

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/")
async def health_check(db: AsyncSession = Depends(get_db)):
    # ИСПРАВЛЕНО: голый except ловил даже SystemExit/KeyboardInterrupt.
    # Сужено до SQLAlchemyError + OSError, чтобы не маскировать реальные баги.
    try:
        await db.execute(text("SELECT 1"))
        return {"status": "ok", "db": "connected"}
    except (SQLAlchemyError, OSError):
        return {"status": "ok", "db": "unavailable"}


# ИСПРАВЛЕНО: дев-эндпоинт для проверки ErrorHandler доступен только в debug.
if settings.debug:
    @router.get("/test-error")
    async def test_error():
        raise ValueError("test unhandled exception")
