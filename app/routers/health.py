from fastapi import APIRouter, Depends

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/")
async def health_check(db: AsyncSession = Depends(get_db)):
    try:
        await db.execute(text("SELECT 1"))
        return {"status": "ok", "db": "connected"}
    except:
        return {"status": "ok", "db": "unavailable"}


@router.get("/test-error")
async def test_error():
    raise ValueError("test unhandled exception")
