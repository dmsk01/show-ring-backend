"""
Глобальные exception-handlers (этап 14, review 2026-05-28).

ИСТОРИЯ: раньше это было BaseHTTPMiddleware (ErrorHandlerMiddleware).
Проблема — он добавлялся как middleware #2, и Starlette оборачивал
запросы так, что ErrorHandler оказывался ВНУТРИ Sanitization, Idempotency,
SecurityHeaders, ProxyHeaders и т. д. — их исключения не ловились и
уходили в дефолтный 500 Starlette без request_id. См. inline-комментарий
в main.py.

Сейчас используем FastAPI `add_exception_handler`: Starlette
ServerErrorMiddleware оборачивает ВСЁ — handlers применяются на самом
верхнем уровне, независимо от порядка user-middleware.

Регистрируем три handler'а:
- (OperationalError, InterfaceError) → 503 — инфраструктурные сбои PG;
- OSError → 503 — сетевые ошибки (broken pipe и т. п.);
- Exception → 500 — общий fallback.

В обоих случаях в теле ответа отдаём request_id (если был установлен
RequestIdMiddleware) — для корреляции с логами.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import InterfaceError, OperationalError

logger = logging.getLogger(__name__)


def _request_id(request: Request) -> str | None:
    """Вытаскиваем request_id, если RequestIdMiddleware успел его установить."""
    return getattr(request.state, "request_id", None)


async def _handle_db_infra(request: Request, exc: Exception) -> JSONResponse:
    # bug_244 audit 2026-05-28: специальная ветка под DB-инфра-сбои
    # (потеря соединения, недоступность PG, statement_timeout).
    # logger.exception сохраняет полный traceback — мы знаем, ЧТО
    # именно сломалось. Ответ 503, чтобы клиент мог делать retry,
    # а balancer — снять инстанс из ротации.
    logger.exception(
        "Database infrastructure error on %s %s",
        request.method,
        request.url.path,
    )
    return JSONResponse(
        status_code=503,
        content={
            "detail": "Database unavailable. Please try again later.",
            "request_id": _request_id(request),
        },
    )


async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    logger.exception(
        "Unhandled error on %s %s",
        request.method,
        request.url.path,
    )
    return JSONResponse(
        status_code=500,
        content={
            "detail": "Internal server error",
            "request_id": _request_id(request),
        },
    )


def register_error_handlers(app: FastAPI) -> None:
    """
    Подключение exception-handlers к FastAPI-приложению. Вызывается
    однажды из main.py. FastAPI хранит handler'ы в порядке регистрации,
    но матчит по типу исключения (наиболее специфичный — выигрывает),
    так что порядок ниже не критичен; держим от конкретного к общему
    ради читаемости.
    """
    app.add_exception_handler(OperationalError, _handle_db_infra)
    app.add_exception_handler(InterfaceError, _handle_db_infra)
    app.add_exception_handler(OSError, _handle_db_infra)
    app.add_exception_handler(Exception, _handle_unexpected)
