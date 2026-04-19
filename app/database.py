from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from .config import settings

# 1. Создание асинхронного движка
# Формат URL: dialect+driver://user:password@host/dbname
engine = create_async_engine(
    settings.database_url,
    echo=True,  # Логирование SQL-запросов
)

# 2. Создание фабрики сессий
# expire_on_commit=False критически важен в async, чтобы объекты 
# оставались доступными после commit без дополнительных запросов к БД.
async_session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False
)

async def get_db():
    async with async_session_factory() as session:
        yield session
