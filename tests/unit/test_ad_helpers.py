"""
Unit-тесты вспомогательных функций рекламного модуля (этап 13).

Здесь нет БД и Redis — только чистые helper'ы.
"""

from __future__ import annotations

from app.services.ad import _hash_user_agent


class TestHashUserAgent:
    def test_none_returns_none(self):
        # На входе нет UA — отдаём None, чтобы не записать в БД пустой
        # хеш (выглядел бы как «настоящий» при поиске дублей).
        assert _hash_user_agent(None) is None

    def test_empty_string_returns_none(self):
        # Пустая строка — тоже отсутствие UA.
        assert _hash_user_agent("") is None

    def test_returns_hex_sha256(self):
        # SHA-256 → 64 hex-символа.
        h = _hash_user_agent("Mozilla/5.0")
        assert h is not None
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_deterministic(self):
        # Один и тот же UA должен давать тот же хеш — иначе дедупликация
        # «банер+ip+ua+тип» не сработает.
        a = _hash_user_agent("Mozilla/5.0 Chrome/130")
        b = _hash_user_agent("Mozilla/5.0 Chrome/130")
        assert a == b

    def test_different_uas_different_hashes(self):
        a = _hash_user_agent("Mozilla/5.0 Chrome/130")
        b = _hash_user_agent("Mozilla/5.0 Firefox/120")
        assert a != b
