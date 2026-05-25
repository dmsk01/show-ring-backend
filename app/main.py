import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from contextlib import asynccontextmanager
from sqlalchemy import text
from app.config import settings
from app.database import engine
from app.middleware.error_handler import ErrorHandlerMiddleware
from app.middleware.request_id import RequestIdMiddleware
from app.middleware.sanitization import SanitizationMiddleware
from app.routers import health, auth, users, references, kennels, dogs, files
from app.routers.admin import references as admin_references
from app.redis import init_redis, close_redis

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ИСПРАВЛЕНО: print → logging, чтобы события поднимались в обработчик логов
    # и не терялись в проде без stdout-захвата.
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        app.state.db_available = True
        logger.info("DB connection OK")
    except Exception as e:
        app.state.db_available = False
        logger.warning("DB unavailable at startup: %s", e)
    await init_redis()
    yield
    await engine.dispose()
    await close_redis()


app = FastAPI(lifespan=lifespan)
app.add_middleware(RequestIdMiddleware)
app.add_middleware(ErrorHandlerMiddleware)
app.add_middleware(SanitizationMiddleware)

# ИСПРАВЛЕНО: CORSMiddleware подключается только если список доменов задан.
# Пустой список = CORS не настраивается (без * по умолчанию) — иначе
# фронт с любого домена мог бы дёргать API.
if settings.cors_allow_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(users.router)
# Этап 3: справочники (публичные GET) + админ-CRUD.
app.include_router(references.router)
app.include_router(admin_references.router)
# Этап 4: питомники, собаки, файлы (MinIO).
app.include_router(kennels.router)
app.include_router(dogs.router)
app.include_router(files.router)
