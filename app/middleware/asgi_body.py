"""
Чтение и подмена тела HTTP-запроса в чистом ASGI-middleware (ревью
2026-10-06, BE-28).

BaseHTTPMiddleware для этого не годится: тело отдаётся дальше через
приватные `request._body`/`request._receive` (так однажды санитизация молча
перестала работать — handler получал исходное тело), а ответ буферизуется
целиком. На уровне ASGI тело — просто поток сообщений `http.request`:
читаем его и отдаём приложению новый `receive`, который сначала вернёт
наше тело, а дальше — сообщения клиента (`http.disconnect`).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]


class ClientDisconnected(Exception):
    """Клиент ушёл, не дослав тело, — обрабатывать нечего."""


async def read_body(receive: Receive) -> bytes:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            raise ClientDisconnected
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            return b"".join(chunks)


def replay_receive(body: bytes, receive: Receive) -> Receive:
    """receive, который сначала отдаёт `body`, затем — исходный поток."""
    sent = False

    async def _receive() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return _receive


def with_content_length(scope: MutableMapping[str, Any], length: int) -> dict:
    """Копия scope с заголовком Content-Length под новое тело."""
    headers = [
        (k, v) for k, v in scope.get("headers", []) if k.lower() != b"content-length"
    ]
    headers.append((b"content-length", str(length).encode("latin-1")))
    return {**scope, "headers": headers}
