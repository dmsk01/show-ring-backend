from fastapi import FastAPI

from contextlib import asynccontextmanager
from sqlalchemy import text
from app.database import engine
from app.middleware.error_handler import ErrorHandlerMiddleware
from app.middleware.request_id import RequestIdMiddleware
from app.middleware.sanitization import SanitizationMiddleware
from app.middleware.progressive_ban import check_rate_limit
from app.routers import health, auth, users
from app.redis import init_redis, close_redis


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        app.state.db_available = True
        print("DB connection OK")
    except Exception as e:
        app.state.db_available = False
        print(f"WARNING: DB unavailable at startup: {e}")
    await init_redis()
    yield
    await engine.dispose()
    await close_redis()


app = FastAPI(lifespan=lifespan)
app.add_middleware(RequestIdMiddleware)
app.add_middleware(ErrorHandlerMiddleware)
app.add_middleware(SanitizationMiddleware)
app.include_router(health.router)
app.include_router(auth.router)
app.include_router(users.router)
