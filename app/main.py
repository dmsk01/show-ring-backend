from fastapi import FastAPI

from contextlib import asynccontextmanager
from sqlalchemy import text
from app.database import engine
from app.routers import health, auth, users
from app.redis import init_redis, close_redis


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        print("DB connection OK")
    except Exception as e:
        print(f"WARNING: DB unavailable at startup: {e}")
    await init_redis()
    yield
    await engine.dispose()
    await close_redis()


app = FastAPI(lifespan=lifespan)
app.include_router(health.router)
app.include_router(auth.router)
app.include_router(users.router)
