"""
Интеграция: доменные исключения (ревью 2026-10-06, BE-32).

Сервисы сигнализировали об ошибках строками ValueError("not_found"), а
каждый роутер держал свою таблицу «код → HTTP-статус». Код, забытый в
таблице, становился 400, а посторонний ValueError (из uuid.UUID() и т. п.)
молча уходил клиенту. Теперь сервис бросает DomainError с нужным статусом,
и его переводит в ответ один глобальный обработчик.
"""

from __future__ import annotations

import pytest
from fastapi import APIRouter

from app.errors import Conflict, DomainError, Forbidden, NotFound, Unprocessable
from app.main import app
from app.routers import shows as shows_router

_router = APIRouter()


@_router.get("/__test/domain/{kind}")
async def _raise(kind: str):
    raise {
        "nf": NotFound("thing_not_found"),
        "fb": Forbidden("forbidden"),
        "cf": Conflict("already_exists"),
        "un": Unprocessable("bad_state"),
        "base": DomainError("generic"),
    }[kind]


@pytest.fixture
def routes():
    before = list(app.router.routes)
    app.include_router(_router)
    try:
        yield
    finally:
        app.router.routes[:] = before


@pytest.mark.parametrize(
    "kind, status, detail",
    [
        ("nf", 404, "thing_not_found"),
        ("fb", 403, "forbidden"),
        ("cf", 409, "already_exists"),
        ("un", 422, "bad_state"),
        ("base", 400, "generic"),
    ],
)
async def test_domain_error_maps_to_status(client, routes, kind, status, detail):
    r = await client.get(f"/__test/domain/{kind}")
    assert r.status_code == status
    assert r.json() == {"detail": detail}


def test_shows_router_has_no_local_error_table():
    assert not hasattr(shows_router, "_raise_for_error")
