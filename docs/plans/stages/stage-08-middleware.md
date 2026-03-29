# Этап 8: Middleware в FastAPI

### Цель

Понять систему middleware в FastAPI: порядок выполнения, практические паттерны, написание собственного middleware.

### Теория: что такое Middleware

Middleware — это обёртка вокруг каждого HTTP-запроса. Код выполняется **до** и **после** обработки каждого запроса.

```
Запрос -> [Middleware 1] -> [Middleware 2] -> [Роутер] -> [Middleware 2] -> [Middleware 1] -> Ответ
```

Порядок важен: middleware регистрируются **снаружи внутрь**. Первый зарегистрированный — самый внешний.

### Паттерн 1: Логирование запросов

```python
# app/middleware/logging.py
import time
import logging
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)

class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """
    Логирует каждый HTTP запрос: метод, путь, статус, время выполнения.
    """
    async def dispatch(self, request: Request, call_next) -> Response:
        start_time = time.perf_counter()

        # Пропускаем health-check чтобы не засорять логи
        if request.url.path in ("/health", "/ready"):
            return await call_next(request)

        logger.info(
            "Request started",
            extra={
                "method": request.method,
                "path": request.url.path,
                "client": request.client.host if request.client else "unknown",
            }
        )

        response = await call_next(request)
        duration = time.perf_counter() - start_time

        logger.info(
            "Request completed",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": round(duration * 1000, 2),
            }
        )

        response.headers["X-Process-Time"] = str(round(duration * 1000, 2))
        return response
```

### Паттерн 2: Обработка ошибок

```python
# app/middleware/error_handler.py
import logging
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

class ErrorHandlerMiddleware(BaseHTTPMiddleware):
    """
    Перехватывает необработанные исключения.
    Возвращает единообразный JSON вместо 500 Internal Server Error.
    """
    async def dispatch(self, request: Request, call_next):
        try:
            return await call_next(request)
        except Exception as e:
            logger.exception(
                "Unhandled exception",
                extra={"path": request.url.path, "error": str(e)}
            )
            return JSONResponse(
                status_code=500,
                content={
                    "error": "internal_error",
                    "message": "Внутренняя ошибка сервера",
                }
            )
```

### Паттерн 3: Request ID для трассировки

```python
# app/middleware/request_id.py
import uuid
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

class RequestIDMiddleware(BaseHTTPMiddleware):
    """
    Генерирует уникальный ID для каждого запроса.
    Позволяет связать все логи одного запроса.
    """
    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        request.state.request_id = request_id

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response
```

### Регистрация middleware в main.py

```python
# app/main.py
from app.middleware.request_id import RequestIDMiddleware
from app.middleware.error_handler import ErrorHandlerMiddleware
from app.middleware.logging import RequestLoggingMiddleware

app = FastAPI()

# Порядок важен! Первый добавленный — самый внешний
app.add_middleware(RequestLoggingMiddleware)   # 1. Логирование (внешний)
app.add_middleware(ErrorHandlerMiddleware)     # 2. Ошибки (средний)
app.add_middleware(RequestIDMiddleware)        # 3. Request ID (внутренний)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)
```

### CORS подробнее

```python
# CORS (Cross-Origin Resource Sharing) — механизм безопасности браузера.
# Без него фронтенд на localhost:5173 не сможет обращаться к API на localhost:8000.

# ❌ Типичная ошибка — allow_origins=["*"] в production
# ✅ Всегда указывать конкретные домены
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,  # Из конфигурации
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)
```

### Что делать

1. Создать `app/middleware/` пакет
2. Реализовать `RequestLoggingMiddleware`
3. Реализовать `RequestIDMiddleware`
4. Зарегистрировать middleware в `app/main.py`
5. Проверить через curl что заголовки `X-Process-Time` и `X-Request-ID` возвращаются
