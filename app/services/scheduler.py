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
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import delete, func, select, update

from app import redis as redis_module
from app.database import async_session_factory
from app.models.classified import Classified, ClassifiedStatus
from app.models.task import Task, TaskStatusEnum
from app.models.user import RefreshToken
from app.repositories import outbox as outbox_repo
from app.schemas.task import TaskMessage

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Distributed lock (bug_236 audit 2026-05-28)
# ---------------------------------------------------------------------
#
# APScheduler стартует в lifespan каждого API-инстанса. При replicas>1
# каждая cron-задача срабатывала на всех инстансах одновременно:
# - requeue_stuck_tasks ставил по N копий каждой stuck-задачи в outbox,
# - archive_old_classifieds дублировал UPDATE'ы,
# - cleanup_refresh_tokens — три параллельных DELETE'а (безопасно, но шум).
# Решение: SET NX EX как distributed mutex. Только один инстанс
# в момент времени делает работу; остальные тихо завершаются.
#
# fail-CLOSED: если Redis недоступен — пропускаем запуск. Это
# консервативно (лучше пропустить тик, чем продублировать запись в
# outbox), но осознанно. Для прода рекомендация: либо отдельный
# scheduler-контейнер, либо гарантированно доступный Redis.

_LOCK_TTL_SECONDS = 300

# Lua-скрипт «compare-and-delete»: освобождаем lock только если он
# принадлежит нам. Без этого истёкший по TTL lock другого инстанса
# мог бы быть случайно удалён, что нарушает взаимное исключение.
_LOCK_RELEASE_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""


@asynccontextmanager
async def _scheduler_lock(job_name: str, ttl_seconds: int = _LOCK_TTL_SECONDS):
    """
    Контекст-менеджер «один-в-один-момент» для scheduler-задач.

    Yields True, если lock взят (задачу нужно выполнить), False —
    если кто-то другой уже работает (или Redis недоступен).
    """
    rc = redis_module.redis_client
    if rc is None:
        logger.warning(
            "Scheduler job %s skipped: Redis unavailable "
            "(fail-closed to prevent duplicate execution on replicas)",
            job_name,
        )
        yield False
        return

    lock_key = f"scheduler:lock:{job_name}"
    # nonce — наш «owner-id»; используется при освобождении, чтобы не
    # снять чужой lock после истечения TTL.
    nonce = str(uuid.uuid4())
    try:
        acquired = await rc.set(lock_key, nonce, nx=True, ex=ttl_seconds)
    except Exception:  # noqa: BLE001 — Redis может быть в плохом состоянии
        logger.warning(
            "Scheduler job %s skipped: Redis SET failed", job_name,
            exc_info=True,
        )
        yield False
        return

    if not acquired:
        logger.info(
            "Scheduler job %s skipped: lock held by another instance",
            job_name,
        )
        yield False
        return

    try:
        yield True
    finally:
        try:
            await rc.eval(_LOCK_RELEASE_SCRIPT, 1, lock_key, nonce)
        except Exception:  # noqa: BLE001
            # Не критично — lock сам истечёт через TTL. Логируем для
            # видимости, но не пробрасываем (мы в finally).
            logger.exception(
                "Scheduler job %s: failed to release lock", job_name,
            )


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

    # Каждые 10 минут — пересинхронизация подвисших задач. Часто, чтобы
    # пользователь не ждал часами после крэша воркера.
    sched.add_job(
        requeue_stuck_tasks,
        CronTrigger(minute="*/10"),
        id="requeue_stuck_tasks",
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
    async with _scheduler_lock("cleanup_refresh_tokens") as acquired:
        if not acquired:
            return
        async with async_session_factory() as db:
            stmt = delete(RefreshToken).where(
                (RefreshToken.expires_at < datetime.now(timezone.utc))
                | (RefreshToken.is_revoked.is_(True))
            )
            result = await db.execute(stmt)
            await db.commit()
            deleted = getattr(result, "rowcount", 0)
            logger.info("cleanup_refresh_tokens: deleted %s rows", deleted)


async def requeue_stuck_tasks() -> None:
    """
    Возвращает в pending задачи, висящие в processing дольше 1 часа,
    и через outbox перепубликует их в RabbitMQ.

    Сценарий: воркер взял задачу через claim_task (UPDATE → processing),
    после чего упал/был убит OOM-killer'ом. Сама задача в очереди уже
    ack'нута (или потеряна), новый воркер её не возьмёт без переподачи.

    1 час — компромисс: PDF-каталог тысячи собак или batch дипломов
    могут идти десятки минут; раньше срабатывать опасно (поломаем
    легитимную работу). Если когда-то появятся задачи >1ч, поле
    `attempts` уже считается — можно ограничить максимальное число
    повторов (TODO).

    Re-publish через outbox: добавляем запись в outbox_events в той же
    транзакции, что и UPDATE status. Outbox-воркер подберёт и
    опубликует — гарантия "статус pending ⇔ событие в очереди".
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    async with _scheduler_lock("requeue_stuck_tasks") as acquired:
        if not acquired:
            return
        async with async_session_factory() as db:
            # SELECT + UPDATE построчно — нужны type и payload для
            # правильной публикации в очередь конкретного типа.
            select_stmt = select(Task).where(
                Task.status == TaskStatusEnum.processing,
                Task.updated_at < cutoff,
            )
            stuck = (await db.execute(select_stmt)).scalars().all()
            if not stuck:
                return

            for task in stuck:
                task.status = TaskStatusEnum.pending
                # Имя очереди = task.type (см. константы в routers/documents.py
                # и worker/handlers/*; договорённость — type строки совпадают
                # с queue_name для DB-backed задач). Для типов, которых нет в
                # этой карте, перепубликацию пропускаем — UPDATE → pending
                # всё равно сделан, можно перезапустить вручную.
                queue_name = _QUEUE_FOR_TASK_TYPE.get(task.type)
                if queue_name is None:
                    logger.warning(
                        "requeue_stuck_tasks: no queue for type %s (task %s)",
                        task.type,
                        task.id,
                    )
                    continue
                # Тело сообщения — то же, что и в роутерах при первичной
                # публикации (см. routers/documents._publish_task).
                # model_dump(mode="json") сериализует UUID/datetime в строки,
                # чтобы payload корректно лёг в JSONB и потом был
                # десериализован воркером через TaskMessage.from_json.
                message = TaskMessage(
                    task_id=task.id,
                    action=task.type,
                    payload=task.payload,
                )
                await outbox_repo.enqueue(
                    db,
                    exchange=None,  # default exchange, routing_key = queue_name
                    routing_key=queue_name,
                    payload=message.model_dump(mode="json"),
                )

            await db.commit()
            logger.warning(
                "requeue_stuck_tasks: re-queued %s tasks via outbox",
                len(stuck),
            )


# Map task.type → RabbitMQ queue. Должен совпадать с константами в
# routers/documents.py (DOCUMENT_TASK_QUEUE) и worker/main.py. Когда
# появятся новые DB-backed типы задач, добавляем сюда; единая точка
# изменения избавляет от рассыпанных строковых литералов.
_QUEUE_FOR_TASK_TYPE: dict[str, str] = {
    "generate_catalog": "document_task",
    "generate_diploma": "document_task",
    "generate_diplomas_batch": "document_task",
}


async def archive_old_classifieds() -> None:
    """
    Переводит активные объявления старше 90 дней в archived.

    90 дней — типичный TTL для "продажи щенков". Если объявление
    провисело так долго и не закрыто автором — скорее всего щенки
    уже распроданы либо объявление потеряло актуальность.

    ИСПРАВЛЕНО (review 2026-05-28): раньше cutoff проверялся по
    `created_at`. Это игнорировало активность автора: если объявление
    создано 95 дней назад, но 2 дня назад в нём правили цену, оно
    всё равно архивировалось. Теперь учитываем оба времени и берём
    более позднее — свежие правки задерживают архивацию.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=90)
    async with _scheduler_lock("archive_classifieds") as acquired:
        if not acquired:
            return
        async with async_session_factory() as db:
            stmt = (
                update(Classified)
                .where(
                    Classified.status == ClassifiedStatus.active,
                    # GREATEST(created_at, updated_at) — стандартный PG-
                    # приём. SQLAlchemy не знает GREATEST как ANSI-функцию,
                    # делаем через generic func.greatest (PG-native).
                    func.greatest(
                        Classified.created_at, Classified.updated_at
                    ) < cutoff,
                )
                .values(status=ClassifiedStatus.archived)
            )
            result = await db.execute(stmt)
            await db.commit()
            archived = getattr(result, "rowcount", 0)
            logger.info("archive_classifieds: archived %s rows", archived)
