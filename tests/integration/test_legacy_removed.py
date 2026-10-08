"""
Интеграция: учебный legacy-код удалён (ревью 2026-10-06, BE-35).

- POST /tasks/send и PUT /tasks/{id}/status (учебный book_handler, in-memory
  хранилище задач) больше не существуют: in-memory состояние на нескольких
  uvicorn-воркерах было неконсистентным, а ручки — лишней поверхностью атаки.
- GET /tasks/{id} читает только БД; не-UUID id — 422 валидации, а не поиск
  в памяти процесса.
"""

from __future__ import annotations

import uuid

from tests.integration.checkin_helpers import auth, make_api_user


async def test_legacy_task_endpoints_removed(client):
    r = await client.post("/tasks/send", json={"message": "x"})
    assert r.status_code in (404, 405)
    r = await client.put(f"/tasks/{uuid.uuid4()}/status", json={"status": "done"})
    assert r.status_code in (404, 405)


async def test_task_status_requires_uuid(client):
    _, token = await make_api_user(client)
    r = await client.get("/tasks/not-a-uuid", headers=auth(token))
    assert r.status_code == 422
