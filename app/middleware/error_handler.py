import logging
from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import InterfaceError, OperationalError
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)


class ErrorHandlerMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        try:
            return await call_next(request)
        # bug_244 audit 2026-05-28: специальная ветка под DB-инфра-сбои
        # (потеря соединения, недоступность PG, statement_timeout).
        # logger.exception сохраняет полный traceback — мы знаем, ЧТО
        # именно сломалось, в отличие от прежнего HTTPException(503) в
        # get_db, который терял стек. Ответ — 503, чтобы клиент мог
        # делать retry, а balancer — снять инстанс из ротации.
        except (OperationalError, InterfaceError, OSError):
            logger.exception(
                "Database infrastructure error on %s %s",
                request.method,
                request.url.path,
            )
            return JSONResponse(
                status_code=503,
                content={
                    "detail": "Database unavailable. Please try again later.",
                    "request_id": getattr(request.state, "request_id", None),
                },
            )
        except Exception:
            logger.exception(
                "Unhandled error on %s %s",
                request.method,
                request.url.path,
            )
            return JSONResponse(
                status_code=500,
                content={
                    "detail": "Internal server error",
                    "request_id": getattr(request.state, "request_id", None),
                },
            )
