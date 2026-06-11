"""
Юнит-тесты парсера Accept-Language (app/utils/locale.py).

Контракт: поддерживаем только ru/en; язык не определён или не
поддерживается → дефолт "ru". Выбор — по максимальному q-весу среди
поддерживаемых; при равных q побеждает первый по порядку в заголовке.
"""

from __future__ import annotations

import pytest

from app.utils.locale import resolve_locale


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        # Заголовка нет / пустой → дефолт.
        (None, "ru"),
        ("", "ru"),
        ("   ", "ru"),
        # Простые случаи.
        ("en", "en"),
        ("ru", "ru"),
        ("EN", "en"),
        # Региональные подтеги сводятся к основному языку.
        ("en-US,en;q=0.9", "en"),
        ("ru-RU", "ru"),
        # Неподдерживаемые языки игнорируются (фолбэк на дефолт).
        ("fr", "ru"),
        ("de-DE,fr;q=0.8", "ru"),
        ("*", "ru"),
        # Выбор по q-весам среди поддерживаемых.
        ("fr,en;q=0.5", "en"),
        ("en;q=0.4,ru", "ru"),
        ("ru;q=0.3,en;q=0.7", "en"),
        # q=0 означает "язык неприемлем" — токен исключается.
        ("en;q=0", "ru"),
        # Битые токены не роняют парсер.
        (";;;", "ru"),
        ("en;q=abc,ru;q=0.5", "ru"),
        # Равные q — первый по порядку.
        ("en,ru", "en"),
        ("ru,en", "ru"),
    ],
)
def test_resolve_locale(header: str | None, expected: str) -> None:
    assert resolve_locale(header) == expected
