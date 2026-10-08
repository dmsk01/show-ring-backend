"""Анонимные списки отдают ограниченную страницу (план защиты 2026-10-05)."""

from __future__ import annotations

from tests.integration.checkin_helpers import auth, make_api_user


async def test_anonymous_list_page_is_capped(client):
    r = await client.get("/kennels", params={"per_page": 200})
    assert r.status_code == 200
    assert r.json()["per_page"] == 50

    _, token = await make_api_user(client)
    r = await client.get("/kennels", params={"per_page": 200}, headers=auth(token))
    assert r.json()["per_page"] == 200


async def test_capped_lists(client):
    for path in ("/dogs", "/litters", "/shows", "/classifieds"):
        r = await client.get(path, params={"per_page": 200})
        assert r.status_code == 200, (path, r.text)
        assert r.json()["per_page"] == 50, path
