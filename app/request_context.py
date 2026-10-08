"""
request_id текущего запроса для логов (ревью 2026-10-06, BE-22).

RequestIdMiddleware кладёт id в contextvar, а фабрика LogRecord добавляет
его в КАЖДУЮ запись лога как атрибут request_id. Так traceback ошибки 500
можно найти по request_id из тела ответа. JSONFormatter выводит атрибут
автоматически (как любое extra-поле), текстовый формат — явно.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

_installed = False


def install_log_record_factory() -> None:
    """Идемпотентно добавить request_id во все LogRecord процесса."""
    global _installed
    if _installed:
        return
    base_factory = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = base_factory(*args, **kwargs)
        if not hasattr(record, "request_id"):
            record.request_id = request_id_var.get() or "-"
        return record

    logging.setLogRecordFactory(factory)
    _installed = True
