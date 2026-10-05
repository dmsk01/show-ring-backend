"""
Лимиты на создание контента по пользователю (план защиты 2026-10-05, этап 2).

Общий лимит nginx (20 запросов/с с IP) не мешает одному аккаунту залить
сотни обращений или объявлений. Лимит считается по user_id, а не по IP:
смена адреса его не обходит, а соседи по NAT друг другу не мешают.
"""

from __future__ import annotations

from tests.integration.checkin_helpers import auth, make_api_user


async def test_support_tickets_limited_per_user(client):
    _, token = await make_api_user(client)

    for i in range(5):
        r = await client.post(
            "/support/tickets",
            json={"subject": f"Вопрос {i}", "body": "Текст обращения"},
            headers=auth(token),
        )
        assert r.status_code == 201, r.text

    r = await client.post(
        "/support/tickets",
        json={"subject": "Шестой", "body": "Текст обращения"},
        headers=auth(token),
    )
    assert r.status_code == 429
    assert "Retry-After" in r.headers

    # Лимит персональный: другой пользователь с того же IP не задет.
    _, other = await make_api_user(client)
    r = await client.post(
        "/support/tickets",
        json={"subject": "Мой вопрос", "body": "Текст обращения"},
        headers=auth(other),
    )
    assert r.status_code == 201, r.text
