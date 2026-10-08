"""
Unit: гигиена логов (ревью 2026-10-06, BE-12).

- Тело email-задачи (HTML со ссылками-токенами verify/confirm-email-change)
  не должно попадать в лог при ошибке обработки.
- Email и телефоны в security-логах маскируются.
- Воркер настраивает логирование через setup_logging (JSON в проде),
  а не через logging.basicConfig.
"""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from app.utils.log_mask import mask_email, mask_phone


def test_mask_email():
    assert mask_email("ivan.petrov@mail.ru") == "i***@mail.ru"
    assert mask_email("a@b.c") == "a***@b.c"
    assert mask_email(None) == "-"
    assert mask_email("not-an-email") == "***"


def test_mask_phone():
    assert mask_phone("+79991234567") == "+7******4567"
    assert mask_phone(None) == "-"
    assert mask_phone("123") == "***"


class _FakeMessage:
    def __init__(self, body: bytes) -> None:
        self.body = body

    @asynccontextmanager
    async def process(self, requeue: bool = False):
        yield


@asynccontextmanager
async def _fake_session():
    yield object()


@pytest.mark.parametrize(
    "handler_name, target",
    [("on_email_task", "process_email_task"), ("on_topic_event", "process_event")],
)
async def test_worker_failure_does_not_log_message_body(
    monkeypatch, caplog, handler_name, target
):
    from worker import main as worker_main

    monkeypatch.setattr(worker_main, target, AsyncMock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(worker_main, "async_session_factory", _fake_session)
    monkeypatch.setattr(worker_main, "_topic_publish_channel", object())
    body = (
        b'{"notification_id": "00000000-0000-0000-0000-000000000001",'
        b' "html_body": "<a href=\\"https://x/verify-email?token=SECRET-TOKEN\\">",'
        b' "to_email": "victim@example.com"}'
    )
    with caplog.at_level(logging.INFO):
        await getattr(worker_main, handler_name)(_FakeMessage(body))
    assert "SECRET-TOKEN" not in caplog.text
    assert "victim@example.com" not in caplog.text
    assert "boom" in caplog.text  # сам факт ошибки виден


async def test_login_failure_log_masks_email(monkeypatch, caplog):
    from app.repositories import user as user_repo
    from app.services import auth as auth_service

    monkeypatch.setattr(user_repo, "get_user_by_email", AsyncMock(return_value=None))
    monkeypatch.setattr(auth_service, "dummy_verify_password_async", AsyncMock())
    with caplog.at_level(logging.INFO, logger="app.security"):
        with pytest.raises(ValueError):
            await auth_service.login_user(object(), "ivan.petrov@mail.ru", "x")  # type: ignore[arg-type]
    assert "ivan.petrov@mail.ru" not in caplog.text
    assert "i***@mail.ru" in caplog.text


def test_worker_main_uses_setup_logging(monkeypatch):
    from worker import main as worker_main

    called: list[str] = []
    monkeypatch.setattr(worker_main, "setup_logging", lambda: called.append("setup"))
    monkeypatch.setattr(worker_main.asyncio, "run", lambda coro: coro.close())
    monkeypatch.setattr(sys, "argv", ["worker", "--mode", "outbox"])
    worker_main.main()
    assert called == ["setup"]
