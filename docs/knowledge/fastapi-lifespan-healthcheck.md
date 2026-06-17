# FastAPI: Lifespan и Health-check

Два механизма для проверки состояния приложения. Оба могут выполнять `SELECT 1`, но решают разные задачи в разные моменты жизни процесса.

## Lifespan

Запускается **один раз при старте и остановке** процесса. Принимает `@asynccontextmanager`: код до `yield` — startup, после — shutdown.

```python
from contextlib import asynccontextmanager
from sqlalchemy import text
from app.database import engine

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))  # fail-fast: упадём до приёма трафика
    yield
    await engine.dispose()  # корректно закрыть пул соединений

app = FastAPI(lifespan=lifespan)
```

Зачем `SELECT 1` в startup: Docker считает контейнер живым сразу после запуска процесса. Без проверки приложение начнёт принимать запросы, а первый же запрос к БД упадёт с 500. Проверка в lifespan — это **входной контроль**: не прошёл — не запустился.

Зачем `engine.dispose()` в shutdown: asyncpg держит пул открытых соединений. Без явного закрытия при перезапуске контейнера возможны ошибки "connection already closed" или утечки соединений на стороне PostgreSQL.

## Health-check эндпоинт

Вызывается **периодически во время работы** — Docker `HEALTHCHECK`, Kubernetes liveness/readiness probes, внешний мониторинг (UptimeRobot, Grafana и т.д.).

В Show Ring health разведён на **два эндпоинта** с разными статус-кодами (`app/routers/health.py`), потому что у «жив ли я» и «можно ли слать трафик» разные потребители:

```python
@router.get("/")          # GET /health/ — детальный, ВСЕГДА 200
async def health_check(db: AsyncSession = Depends(get_db)):
    # параллельно проверяет db/redis/rabbitmq/minio, каждый → "ok" | "down"
    return {"status": "ok", "components": {"db": db_s, "redis": redis_s, ...}}

@router.get("/ready")     # GET /health/ready — бинарный, 503 если PG down
async def readiness_probe(db: AsyncSession = Depends(get_db)):
    if await _check_db(db) != "ok":
        raise HTTPException(status_code=503, detail={"db": ...})
    return {"status": "ready"}
```

- **`/health/`** — для **дашборда/мониторинга**: всегда 200, чтобы система не перестала опрашивать эндпоинт и видела детали в теле (какой именно компонент `down`). HTTP-код стабилен — деградация читается из JSON, а не из статуса.
- **`/health/ready`** — для **Docker `HEALTHCHECK` и load balancer'ов**: бинарное 200/503. Критичен только PostgreSQL (без БД API не работает); Redis/Rabbit/MinIO могут деградировать, но не выводят инстанс из ротации. Именно `/health/ready` стоит в `HEALTHCHECK` нашего `Dockerfile`.

> **Тонкость с trailing slash.** Роут объявлен `@router.get("/")` при `prefix="/health"`, поэтому канонический путь — `/health/`. Запрос `/health` отдаёт **307-редирект** на `/health/`; в `curl`-проверках добавляй `-L` либо бей сразу в `/health/`. С `-f` без `-L` проверка «проходила» уже на редиректе, не доходя до реального ответа — поэтому `HEALTHCHECK` бьёт в точный `/health/ready` (без редиректа).

## Зачем оба механизма

| | Lifespan (startup) | Health-эндпоинты (runtime) |
|---|---|---|
| Когда | Один раз при старте | Периодически (каждые 15–30 сек) |
| Вопрос | "Могу ли я начать работу?" | "Я всё ещё жив? Можно слать трафик?" |
| Кто вызывает | FastAPI сам при запуске | Docker, Kubernetes, мониторинг |
| При сбое БД | Процесс не стартует | `/health/` → всё ещё 200 (деталь в JSON); `/health/ready` → 503 → контейнер `unhealthy` |

БД может быть доступна при старте и упасть через 2 часа — lifespan это не поймает. Поэтому нужны оба механизма, а health дополнительно разведён на детальный и бинарный.

## Ссылки

- [FastAPI Lifespan Events](https://fastapi.tiangolo.com/advanced/events/)
- [Docker HEALTHCHECK](https://docs.docker.com/reference/dockerfile/#healthcheck)
- [Kubernetes Liveness Probes](https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/)
