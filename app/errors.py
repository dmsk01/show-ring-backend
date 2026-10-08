"""
Доменные исключения сервисного слоя (ревью 2026-10-06, BE-32).

Раньше сервисы бросали ValueError("code"), а каждый роутер переводил код
в HTTP-статус своей таблицей _raise_for_error: код, забытый в таблице,
становился 400, а посторонний ValueError уходил клиенту как «доменная»
ошибка. Теперь статус несёт сам класс исключения, а в ответ его переводит
один глобальный обработчик (app/middleware/error_handler.py).

Наследуются от ValueError — роутеры, ещё не переведённые на новую схему
(except ValueError + _raise_for_error), продолжают работать без изменений.
Контракт ответа прежний: {"detail": "<code>"}.
"""

from __future__ import annotations


class DomainError(ValueError):
    """Ошибка бизнес-правила. code — машиночитаемый detail ответа."""

    status_code = 400

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class NotFound(DomainError):
    status_code = 404


class Forbidden(DomainError):
    status_code = 403


class Conflict(DomainError):
    status_code = 409


class Unprocessable(DomainError):
    """Запрос корректен, но нарушает правило предметной области."""

    status_code = 422
