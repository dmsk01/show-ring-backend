from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
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
    # bug_225 audit 2026-05-28: явный sizing вместо дефолтных 5+10.
    # См. config.db_pool_size для обоснования.
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
)

# 2. Создание фабрики сессий
# expire_on_commit=False критически важен в async, чтобы объекты
# оставались доступными после commit без дополнительных запросов к БД.
async_session_factory = async_sessionmaker(
    bind=engine, class_=AsyncSession, expire_on_commit=False
)


async def get_db():
    # ИСПРАВЛЕНО (bug_244 audit 2026-05-28): убрана обёртка
    # try/except (OperationalError, InterfaceError, OSError) → 503
    # вокруг yield. Раньше любая такая ошибка, поднятая ВНУТРИ
    # handler'а (например, statement_timeout на медленном запросе)
    # конвертировалась в общий 503 с сообщением «Database unavailable»
    # без traceback'а в логах — отладка ломалась. Теперь различение
    # DB-ошибок и общий 503-ответ делает ErrorHandlerMiddleware,
    # сохраняя стек исключения.
    async with async_session_factory() as session:
        yield session
