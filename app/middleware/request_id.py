import uuid

from starlette.datastructures import Headers, MutableHeaders

from app.request_context import install_log_record_factory, request_id_var

install_log_record_factory()


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


class RequestIdMiddleware:
    """
    X-Request-ID для каждого запроса: в request.state, в contextvar (для
    логов) и в заголовок ответа.

    Чистый ASGI (ревью 2026-10-06, BE-28): у BaseHTTPMiddleware запрос шёл
    через отдельную задачу и поток памяти — лишние расходы на каждый запрос.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = _parse_or_new_request_id(
            Headers(scope=scope).get("x-request-id")
        )
        # request.state.request_id — Starlette хранит state в scope["state"].
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_id(message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        # contextvar → атрибут request_id в каждой записи лога (BE-22).
        token = request_id_var.set(request_id)
        try:
            await self.app(scope, receive, send_with_id)
        finally:
            request_id_var.reset(token)
