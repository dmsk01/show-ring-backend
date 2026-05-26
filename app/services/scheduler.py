"""
Планировщик задач (этап 9).

Используется AsyncIOScheduler из APScheduler — он живёт в одном цикле
с FastAPI/ASGI. Альтернатива (отдельный процесс с BlockingScheduler)
проще, но требует доп. контейнер; для dev и среднего prod встроенный
шедулер подходит.

Задачи на этапе 9:
- cleanup_expired_refresh_tokens — раз в сутки убирает мёртвые
  refresh-токены.
- close_overdue_classifieds — раз в сутки переводит "забытые"
  объявления в archived (после X месяцев).

Включается флагом scheduler_enabled в config. На dev по умолчанию
выключен, чтобы тестовые задания не запускались на каждом старте.
"""

from __future__ import annotations

import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import delete, update

from app.database import async_session_factory
from app.models.classified import Classified, ClassifiedStatus
from app.models.user import RefreshToken

logger = logging.getLogger(__name__)


# AsyncIOScheduler — singleton-инстанс. APScheduler не любит, когда
# несколько шедулеров запущены параллельно в одном процессе.
scheduler: AsyncIOScheduler | None = None


def get_scheduler() -> AsyncIOScheduler:
    """
    Возвращает singleton AsyncIOScheduler. Ленивая инициализация
    нужна, чтобы импорт модуля не запускал шедулер (это ломало бы
    тесты, которые импортируют app без lifespan).
    """
    global scheduler
    if scheduler is None:
        scheduler = AsyncIOScheduler()
    return scheduler


async def start_scheduler() -> None:
    """
    Регистрирует задачи и запускает шедулер. Вызывается из lifespan
    при scheduler_enabled=True.
    """
    sched = get_scheduler()

    # Ежедневно в 03:00 — низкая нагрузка ночью.
    sched.add_job(
        cleanup_expired_refresh_tokens,
        CronTrigger(hour=3, minute=0),
        id="cleanup_refresh_tokens",
        # replace_existing=True — на случай повторного старта (например,
        # при горячем reload в dev). Без флага APScheduler кидает
        # ConflictingIdError при попытке добавить задачу с тем же id.
        replace_existing=True,
    )

    # Ежедневно в 03:15 — отдельный слот, чтобы не пересекаться с
    # cleanup'ом токенов.
    sched.add_job(
        archive_old_classifieds,
        CronTrigger(hour=3, minute=15),
        id="archive_classifieds",
        replace_existing=True,
    )

    sched.start()
    logger.info("APScheduler started with %d jobs", len(sched.get_jobs()))


async def stop_scheduler() -> None:
    """
    Корректное завершение шедулера. wait=False — не блокируем shutdown
    ASGI на ожидание текущих задач (они короткие, мы запустим их
    в следующий раз).
    """
    if scheduler is not None and scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("APScheduler stopped")


# ---------------------------------------------------------------------
# Задачи
# ---------------------------------------------------------------------


async def cleanup_expired_refresh_tokens() -> None:
    """
    Удаляет refresh-токены, у которых истёк expires_at, ИЛИ помеченные
    is_revoked. После 24 часов хранить их нет смысла:
    - access-токен короткоживущий, refresh — единственный способ
      возобновить сессию. Истёкший токен бесполезен.
    - Накопление мёртвых токенов раздувает таблицу и индексы.
    """
    async with async_session_factory() as db:
        stmt = delete(RefreshToken).where(
            (RefreshToken.expires_at < datetime.utcnow())
            | (RefreshToken.is_revoked.is_(True))
        )
        result = await db.execute(stmt)
        await db.commit()
        deleted = getattr(result, "rowcount", 0)
        logger.info("cleanup_refresh_tokens: deleted %s rows", deleted)


async def archive_old_classifieds() -> None:
    """
    Переводит активные объявления старше 90 дней в archived.

    90 дней — типичный TTL для "продажи щенков". Если объявление
    провисело так долго и не закрыто автором — скорее всего щенки
    уже распроданы либо объявление потеряло актуальность.
    """
    from datetime import timedelta

    cutoff = datetime.utcnow() - timedelta(days=90)
    async with async_session_factory() as db:
        stmt = (
            update(Classified)
            .where(
                Classified.status == ClassifiedStatus.active,
                Classified.created_at < cutoff,
            )
            .values(status=ClassifiedStatus.archived)
        )
        result = await db.execute(stmt)
        await db.commit()
        archived = getattr(result, "rowcount", 0)
        logger.info("archive_classifieds: archived %s rows", archived)
