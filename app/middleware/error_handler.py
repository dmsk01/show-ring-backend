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

Регистрируем handler'ы (матчинг — по наиболее специфичному классу в MRO
исключения, поэтому порядок регистрации не важен):
- (OperationalError, InterfaceError) → 503 — инфраструктурные сбои PG;
- OSError → 503 — сетевые ошибки (broken pipe и т. п.);
- IntegrityError → 409 — нарушен constraint (UNIQUE / FK / CHECK / NOT NULL);
- DBAPIError → 422 ТОЛЬКО для data-exception (SQLSTATE класс 22: numeric
  overflow, value too long, invalid text representation), иначе → 500;
- Exception → 500 — общий fallback.

Во всех случаях в теле ответа отдаём request_id (если был установлен
RequestIdMiddleware) — для корреляции с логами.

ПОЧЕМУ DBAPIError + SQLSTATE, а не DataError (проверено эмпирически на
asyncpg-диалекте 2026-06-14): asyncpg НЕ классифицирует data-ошибки в
sqlalchemy.exc.DataError — numeric overflow прилетает базовым DBAPIError
(MRO: DBAPIError → StatementError, минуя DatabaseError). Поэтому ловим
DBAPIError и различаем по PG SQLSTATE: класс '22' = data exception → вина
клиента (422); всё остальное под этим хендлером (например ProgrammingError,
SQLSTATE класс '42' — битый SQL) — серверный баг, отдаём 500.

IntegrityError/data-ошибки здесь — сеть безопасности (defense-in-depth):
там, где ошибку ждут, её ловят ЛОКАЛЬНО в сервисе (см. services/dog.py,
kennel.py, reference.py, auth.py) и переводят в доменный ответ. Эти
локальные except'ы срабатывают раньше и остаются в приоритете. Сюда
долетает только непредусмотренное нарушение — раньше оно уходило в 500.

Откат сессии гарантирован get_db: его `async with async_session_factory()`
при пробросе исключения закрывает сессию с rollback ещё до того, как
исключение долетит до этих handler'ов, — соединение возвращается в пул
чистым, БД-объект тут трогать не нужно.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import (
    DBAPIError,
    IntegrityError,
    InterfaceError,
    OperationalError,
)

# PG SQLSTATE класс 22 — "data exception" (numeric overflow, string data
# right truncation, invalid text representation и т. п.). Префикс из 2 цифр.
_PG_SQLSTATE_DATA_EXCEPTION = "22"

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


def _sqlstate(exc: Exception) -> str | None:
    """SQLSTATE исходной DBAPI-ошибки (asyncpg кладёт его в exc.orig.sqlstate)."""
    orig = getattr(exc, "orig", None)
    return getattr(orig, "sqlstate", None)


async def _handle_dbapi_error(request: Request, exc: Exception) -> JSONResponse:
    # DBAPIError ловит и непосредственно базовый DBAPIError (asyncpg так
    # отдаёт numeric overflow), и подклассы без своего handler'а (например
    # ProgrammingError). Различаем по SQLSTATE: класс 22 = data exception —
    # вина клиента (422); остальное — серверный баг, отдаём через общий 500.
    sqlstate = _sqlstate(exc)
    if sqlstate is not None and sqlstate.startswith(_PG_SQLSTATE_DATA_EXCEPTION):
        # logger.warning, а не exception: полный traceback не нужен, но
        # method/path/SQLSTATE помогают понять, какое поле недопроверено в
        # схеме (как было с price NUMERIC(10,2), SQLSTATE 22003).
        logger.warning(
            "DB data error on %s %s (SQLSTATE=%s): %s",
            request.method,
            request.url.path,
            sqlstate,
            exc,
        )
        return JSONResponse(
            status_code=422,
            content={
                "detail": "Invalid value: a field is out of range or malformed.",
                "request_id": _request_id(request),
            },
        )
    # Не data-exception (битый SQL, неизвестный DBAPI-сбой) — это наш баг.
    return await _handle_unexpected(request, exc)


async def _handle_integrity_error(
    request: Request, exc: Exception
) -> JSONResponse:
    # Нарушен constraint БД (UNIQUE / FK / CHECK / NOT NULL), не пойманный
    # локально в сервисе. 409 Conflict — состояние данных не позволяет
    # выполнить запрос. detail намеренно общий, чтобы не раскрывать имена
    # constraint'ов и структуру БД.
    logger.warning(
        "DB integrity error on %s %s: %s",
        request.method,
        request.url.path,
        exc,
    )
    return JSONResponse(
        status_code=409,
        content={
            "detail": "Conflict: the request violates a data constraint.",
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
    app.add_exception_handler(IntegrityError, _handle_integrity_error)
    app.add_exception_handler(DBAPIError, _handle_dbapi_error)
    app.add_exception_handler(Exception, _handle_unexpected)
