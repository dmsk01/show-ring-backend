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
