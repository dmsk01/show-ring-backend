import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from contextlib import asynccontextmanager
from sqlalchemy import text
from app.config import settings
from app.database import engine
from app.logging_config import setup_logging
from app.middleware.error_handler import register_error_handlers
from app.middleware.idempotency import IdempotencyMiddleware
from app.middleware.proxy_headers import ProxyHeadersMiddleware
from app.middleware.request_id import RequestIdMiddleware
from app.middleware.sanitization import SanitizationMiddleware
from app.middleware.security_headers import SecurityHeadersMiddleware
from app.routers import (
    ads,
    auth,
    classifieds,
    documents,
    dogs,
    files,
    health,
    kennels,
    litters,
    notifications,
    posts,
    references,
    results,
    shows,
    support,
    tasks,
    users,
)
from app.routers.admin import references as admin_references
from app.routers.admin import analytics as admin_analytics
from app.routers.admin import moderation as admin_moderation
from app.redis import init_redis, close_redis
from app.services.rabbit import rabbit_service
from app.services.scheduler import start_scheduler, stop_scheduler

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # setup_logging — первое действие lifespan, чтобы все последующие
    # log-сообщения уже шли в выбранный формат (JSON в prod, текст в dev).
    # Идемпотентно: переинициализация при reload не дублирует хендлеры.
    setup_logging()
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        app.state.db_available = True
        logger.info("DB connection OK")
    except Exception as e:
        app.state.db_available = False
        logger.warning("DB unavailable at startup: %s", e)
    await init_redis()
    # RabbitMQ подключаем с graceful fallback: если брокер недоступен,
    # API всё равно поднимается — задачи документов будут создаваться
    # в БД, но публикация в очередь упадёт с warning'ом (см.
    # routers/documents._publish_task). Так dev-окружение не ломается,
    # если кто-то забыл поднять rabbit.
    try:
        await rabbit_service.connect(settings.rabbitmq_url)
        app.state.rabbit_available = True
        logger.info("RabbitMQ connected")
    except Exception as e:
        app.state.rabbit_available = False
        logger.warning("RabbitMQ unavailable at startup: %s", e)
    # APScheduler по флагу — на dev обычно выключен, чтобы тестовые
    # cron-задачи не запускались. Включается через .env.
    if settings.scheduler_enabled:
        try:
            await start_scheduler()
        except Exception as e:
            logger.warning("Scheduler failed to start: %s", e)
    yield
    await engine.dispose()
    await close_redis()
    try:
        await rabbit_service.close()
    except Exception as e:
        logger.warning("RabbitMQ close failed: %s", e)
    try:
        await stop_scheduler()
    except Exception as e:
        logger.warning("Scheduler stop failed: %s", e)


app = FastAPI(lifespan=lifespan)
# ИСПРАВЛЕНО (review 2026-05-28): обработка ошибок вынесена в
# FastAPI exception handlers (см. app/middleware/error_handler.py).
# Раньше это был BaseHTTPMiddleware, добавлявшийся 2-м — Starlette
# оборачивал middleware в обратном порядке, и ErrorHandler оказывался
# ВНУТРИ Sanitization/Idempotency/ProxyHeaders. Их исключения не
# ловились → дефолтный 500 без request_id. exception_handler работает
# на уровне ServerErrorMiddleware (обёртка над всем стеком), охватывает
# любые middleware.
register_error_handlers(app)

# Порядок middleware важен: FastAPI применяет их в обратном порядке
# добавления к запросу. Значит первым ВЫПОЛНЯЕТСЯ последний добавленный.
# Идём от "ближе к handler'у" → "ближе к сети":
#   1. RequestId      — добавить ID до всего остального (логи)
#   2. Sanitization   — чистить тело до бизнес-логики
#   3. Idempotency    — ближе к сети, чем Sanitization, поэтому
#                       ВЫПОЛНЯЕТСЯ ПЕРЕД ней: body_hash считается по
#                       СЫРОМУ телу. Это корректно — hash стабилен между
#                       ретраями клиента, а handler всё равно получает
#                       очищенное тело (Sanitization выполняется
#                       внутреннее и перезаписывает _body после нас).
#   4. SecurityHeaders — на ответ всегда, последним в pipeline
#   5. ProxyHeaders   — ВЫПОЛНЯЕТСЯ ПЕРВЫМ (сетевой уровень): подменяем
#                       client IP до того, как rate-limit/ad-fraud его
#                       прочитают.
#   6. TrustedHost    — тоже сетевой: отбиваем Host injection раньше всех.
app.add_middleware(RequestIdMiddleware)
app.add_middleware(SanitizationMiddleware)
app.add_middleware(IdempotencyMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(ProxyHeadersMiddleware)
# TrustedHost доступен в FastAPI/Starlette из коробки. Применяется
# только когда allowed_hosts задан — на dev пустой список = всё пускаем.
if settings.allowed_hosts:
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts
    )

# ИСПРАВЛЕНО: CORSMiddleware подключается только если список доменов задан.
# Пустой список = CORS не настраивается (без * по умолчанию) — иначе
# фронт с любого домена мог бы дёргать API.
#
# ИСПРАВЛЕНО (review 2026-05-28): allow_methods/allow_headers ранее
# были ["*"]. По CORS-спецификации wildcards НЕ разрешены при
# allow_credentials=True — Chrome/Firefox блокируют такие preflight'ы.
# Перечисляем явно методы и заголовки, которые реально использует
# фронт. При появлении новых кастомных заголовков расширяем список.
if settings.cors_allow_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "Idempotency-Key",
            "X-Request-ID",
        ],
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
# Этап 5: помёты и доска объявлений (полнотекстовый поиск).
app.include_router(litters.router)
app.include_router(classifieds.router)
# Этап 6: выставки (создание, судьи, ринги, записи).
app.include_router(shows.router)
# Этап 7: результаты, титулы, публикация.
app.include_router(results.router)
app.include_router(results.publish_router)
# Этап 8: генерация документов (PDF через RabbitMQ + воркер).
app.include_router(documents.router)
# tasks: GET статуса (DB-backed + legacy in-memory fallback), download
# скачивает PDF из MinIO для done-задач.
app.include_router(tasks.router)
# Этап 9: уведомления и подписки.
app.include_router(notifications.router)
# Этап 10: рекламный модуль (кампании, баннеры, serve, events, stats).
app.include_router(ads.router)
# Этап 12: админ-аналитика и модерация.
app.include_router(admin_analytics.router)
app.include_router(admin_analytics.show_report_router)
app.include_router(admin_moderation.router)
# Этап 11: онлайн-поддержка (тикеты + WebSocket чат).
app.include_router(support.router)
# Этап 17: блог (публичный read + write для admin/organizer).
app.include_router(posts.router)
