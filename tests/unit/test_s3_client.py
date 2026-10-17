"""Общий S3-клиент процесса (ревью 2026-10-06, BE-36)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from app.services import file_storage


class _Factory:
    """Подменяет _new_client: считает открытые и закрытые клиенты."""

    def __init__(self) -> None:
        self.opened = 0
        self.closed = 0

    def __call__(self):
        @asynccontextmanager
        async def _cm():
            self.opened += 1
            client = object()
            try:
                yield client
            finally:
                self.closed += 1

        return _cm()


@pytest.fixture
def factory(monkeypatch):
    f = _Factory()
    monkeypatch.setattr(file_storage, "_new_client", f)
    yield f
    # Не оставляем общий клиент другим тестам.
    file_storage._shared_client = None
    file_storage._shared_loop = None
    file_storage._shared_stack = None


async def test_without_init_each_call_gets_temporary_client(factory):
    async with file_storage._s3_client() as a:
        pass
    async with file_storage._s3_client() as b:
        pass
    assert a is not b
    assert factory.opened == factory.closed == 2


async def test_shared_client_is_reused_and_not_closed_per_call(factory):
    await file_storage.init_s3()
    async with file_storage._s3_client() as a:
        pass
    async with file_storage._s3_client() as b:
        pass
    assert a is b
    assert factory.opened == 1
    assert factory.closed == 0

    await file_storage.close_s3()
    assert factory.closed == 1
    assert file_storage._shared_client is None


async def test_init_twice_keeps_one_client(factory):
    await file_storage.init_s3()
    await file_storage.init_s3()
    assert factory.opened == 1
    await file_storage.close_s3()


async def test_close_without_init_is_noop(factory):
    await file_storage.close_s3()
    assert factory.closed == 0


async def test_shared_client_not_used_in_other_event_loop(factory):
    await file_storage.init_s3()
    shared = file_storage._shared_client

    async def _use() -> object:
        async with file_storage._s3_client() as s3:
            return s3

    # Клиент aiobotocore привязан к loop'у, в котором открыт: в другом
    # loop'е (отдельный поток) должен создаться временный клиент.
    other = await asyncio.to_thread(asyncio.run, _use())
    assert other is not shared
    assert factory.opened == 2
    await file_storage.close_s3()
