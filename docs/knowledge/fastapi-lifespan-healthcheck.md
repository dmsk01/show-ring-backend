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

```python
@router.get("/")
async def health_check(db: AsyncSession = Depends(get_db)):
    try:
        await db.execute(text("SELECT 1"))
        return {"status": "ok", "db": "connected"}
    except Exception:
        return {"status": "ok", "db": "unavailable"}
```

Почему `status: ok` даже при `db: unavailable`: HTTP-статус 200 нужен, чтобы мониторинг получил данные. Если вернуть 503, некоторые системы перестанут опрашивать эндпоинт и потеряют детали. Сам факт недоступности БД виден в теле ответа.

## Зачем оба механизма

| | Lifespan (startup) | GET /health |
|---|---|---|
| Когда | Один раз при старте | Периодически (каждые 30 сек) |
| Вопрос | "Могу ли я начать работу?" | "Я всё ещё жив?" |
| Кто вызывает | FastAPI сам при запуске | Docker, Kubernetes, мониторинг |
| При сбое | Процесс не стартует | Контейнер перезапускается |

БД может быть доступна при старте и упасть через 2 часа — lifespan это не поймает. Поэтому нужны оба.

## Ссылки

- [FastAPI Lifespan Events](https://fastapi.tiangolo.com/advanced/events/)
- [Docker HEALTHCHECK](https://docs.docker.com/reference/dockerfile/#healthcheck)
- [Kubernetes Liveness Probes](https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/)
