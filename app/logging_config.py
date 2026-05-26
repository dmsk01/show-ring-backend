"""
Конфигурация логирования (этап 14, production-readiness).

Две схемы:
- dev: человекочитаемый текст с временем и уровнем.
- prod: одна JSON-строка на запись — удобно для ELK/Loki, без парсинга.

Контекст добавляется через стандартные logging.Logger.extra:
    logger.info("ticket created", extra={"ticket_id": str(tid)})
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

from app.config import settings


# Поля LogRecord, которые НЕ нужно дублировать в JSON-выводе — они есть
# в каждом record по умолчанию и забивают логи шумом.
_RESERVED_FIELDS = {
    "name", "msg", "args", "levelname", "levelno", "pathname",
    "filename", "module", "exc_info", "exc_text", "stack_info",
    "lineno", "funcName", "created", "msecs", "relativeCreated",
    "thread", "threadName", "processName", "process",
    "taskName", "message",
}


class JSONFormatter(logging.Formatter):
    """
    Одна JSON-строка на запись. Кастомные поля из extra=... попадают
    на верхний уровень — Loki/ELK сразу делают по ним label/field.
    """

    def format(self, record: logging.LogRecord) -> str:
        # Базовый набор полей. timestamp в ISO 8601 + UTC — стандарт
        # для аггрегаторов; без него Loki не парсит время автоматически.
        data: dict = {
            "ts": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Стектрейс — отдельным полем "exc". Не одной строкой — Loki
        # умеет фильтровать по существованию ключа exc.
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)

        # Все кастомные поля extra. Прокидываем как есть, json.dumps
        # ниже сериализует не-JSON типы через default=str.
        for key, value in record.__dict__.items():
            if key not in _RESERVED_FIELDS and not key.startswith("_"):
                data[key] = value

        return json.dumps(data, default=str, ensure_ascii=False)


def setup_logging() -> None:
    """
    Идемпотентная настройка root-логгера. Безопасна для повторного
    вызова (lifespan FastAPI или ручной reload):
    очищает прежние хендлеры — иначе на reload дубль-вывод.
    """
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    root = logging.getLogger()
    # Дроп существующих хендлеров — иначе при перезагрузке (pytest,
    # uvicorn --reload) получаем дублированные сообщения.
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stdout)
    if settings.log_json:
        handler.setFormatter(JSONFormatter())
    else:
        # Текстовый формат для dev: время + уровень + логгер + сообщение.
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s %(message)s"
            )
        )
    root.addHandler(handler)
    root.setLevel(level)

    # Подавляем шум от часто болтливых библиотек на info-уровне.
    # passlib постит deprecation-варнинги bcrypt на DEBUG; sqlalchemy
    # echo управляется отдельно через настройку engine.
    logging.getLogger("passlib").setLevel(logging.ERROR)
    logging.getLogger("aiormq").setLevel(logging.WARNING)
    logging.getLogger("aio_pika").setLevel(logging.WARNING)
