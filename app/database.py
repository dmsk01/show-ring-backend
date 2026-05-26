from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from fastapi import HTTPException
from sqlalchemy.exc import OperationalError, InterfaceError
from .config import settings

# 1. Создание асинхронного движка
# Формат URL: dialect+driver://user:password@host/dbname
# ИСПРАВЛЕНО: echo=True писало все SQL-запросы и параметры (включая
# хеши паролей и токенов) в stdout — утечка чувствительных данных в логи.
# Управляется флагом debug из настроек, по умолчанию выключено.
engine = create_async_engine(
    settings.database_url,
    echo=settings.debug,
    # pool_pre_ping=True (этап 14): перед выдачей соединения из пула
    # делаем cheap-ping (SELECT 1). Защита от "stale connections": после
    # ребута PG / отключения сети старое соединение в пуле кажется
    # живым, но при использовании вылетает с InterfaceError. pre_ping
    # ловит мёртвые сокеты заранее и пересоздаёт их.
    pool_pre_ping=True,
    # pool_recycle=1800 — закрываем соединения старше 30 минут даже без
    # ошибок. PG/PgBouncer часто имеют свой idle_timeout; recycle
    # синхронизирует SQLAlchemy с этим лимитом.
    pool_recycle=1800,
)

# 2. Создание фабрики сессий
# expire_on_commit=False критически важен в async, чтобы объекты
# оставались доступными после commit без дополнительных запросов к БД.
async_session_factory = async_sessionmaker(
    bind=engine, class_=AsyncSession, expire_on_commit=False
)


async def get_db():
    try:
        async with async_session_factory() as session:
            yield session
    except (OperationalError, InterfaceError, OSError):
        raise HTTPException(
            status_code=503, detail="Database unavailable. Please try again later."
        )
