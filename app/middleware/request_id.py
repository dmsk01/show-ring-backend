import uuid

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware


def _parse_or_new_request_id(header_value: str | None) -> str:
    """
    Валидируем входящий X-Request-ID. Принимаем только если это
    разумный UUID-string; иначе игнорируем и генерируем свой.

    ИСПРАВЛЕНО (bug_245 audit 2026-05-28): раньше middleware
    принимал ЛЮБОЕ значение хедера, включая многострочные строки
    с control-символами. Клиент мог прислать
    `X-Request-ID: <injected log content>\\nFAKE_LINE` и засорять
    логи (log injection). Также валидный, но предсказуемый
    request_id облегчает атаки на кеши/корреляторы. UUID гарантирует
    фиксированный формат и достаточную энтропию.
    """
    if header_value is None:
        return str(uuid.uuid4())
    try:
        # uuid.UUID отвергает любую строку, не соответствующую
        # каноническому формату — и control-символы, и слишком длинные
        # значения. Возвращаем canonical-form, чтобы клиент не мог
        # подсунуть, скажем, верхнерегистровый или с фигурными скобками.
        return str(uuid.UUID(header_value))
    except (ValueError, AttributeError, TypeError):
        return str(uuid.uuid4())


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = _parse_or_new_request_id(
            request.headers.get("X-Request-ID")
        )

        request.state.request_id = request_id

        response = await call_next(request)

        response.headers["X-Request-ID"] = request_id

        return response
