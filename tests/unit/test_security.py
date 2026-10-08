"""
Unit-тесты модуля безопасности (этап 13).

Фокус — функции, которые работают без БД и сети:
- хеширование паролей,
- JWT encode/decode,
- random-токены (refresh, verification).
"""

from __future__ import annotations

import time

import pytest

from app.utils.security import (
    JWTError,
    create_access_token,
    create_refresh_token_value,
    decode_access_token,
    dummy_verify_password,
    generate_verification_token,
    hash_password,
    hash_token,
    validate_password,
    verify_password,
)


# ---------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------


class TestPasswordHashing:
    def test_hash_is_not_plain(self):
        # Хеш не должен совпадать с исходным паролем.
        h = hash_password("super-secret-123")
        assert h != "super-secret-123"
        assert h.startswith("$2")  # bcrypt-префикс

    def test_verify_correct(self):
        h = hash_password("correct-horse")
        assert verify_password("correct-horse", h) is True

    def test_verify_incorrect(self):
        h = hash_password("correct-horse")
        assert verify_password("wrong-horse", h) is False

    def test_hash_is_salted_unique(self):
        # bcrypt с разными солями → одинаковый пароль даёт разные хеши.
        # Защищает от rainbow tables.
        h1 = hash_password("same-password")
        h2 = hash_password("same-password")
        assert h1 != h2

    def test_dummy_verify_runs_without_error(self):
        # Должен молча отработать (используется для constant-time
        # при «пользователь не найден» в логине).
        dummy_verify_password()


class TestValidatePassword:
    def test_short_password_rejected(self):
        with pytest.raises(ValueError, match="8"):
            validate_password("short")

    def test_too_long_password_rejected(self):
        with pytest.raises(ValueError):
            validate_password("a" * 129)

    def test_boundary_min(self):
        # Ровно 8 — валидный (граница включительная).
        validate_password("12345678")

    def test_boundary_max(self):
        # Ровно 72 байта — валидный (предел bcrypt, review 2026-06-10).
        validate_password("a" * 72)

    def test_over_72_bytes_rejected(self):
        # bcrypt хеширует только первые 72 байта — всё дальше молча не
        # влияло на проверку. Для UTF-8 кириллицы порог наступает уже на
        # ~36 символах: 40 символов = 80 байт.
        with pytest.raises(ValueError, match="72"):
            validate_password("п" * 40)

    def test_73_ascii_bytes_rejected(self):
        with pytest.raises(ValueError, match="72"):
            validate_password("a" * 73)


# ---------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------


class TestJWT:
    def test_roundtrip(self):
        token = create_access_token("user-id-123", roles=["admin"])
        payload = decode_access_token(token)
        assert payload["sub"] == "user-id-123"
        assert payload["roles"] == ["admin"]
        assert payload["type"] == "access"

    def test_invalid_signature_rejected(self):
        # Меняем символ В СЕРЕДИНЕ сигнатуры (не на границе): в URL-safe
        # base64 граничные символы могут попадать на padding и не менять
        # содержимое, тогда подпись остаётся валидной. Середина — гарантия,
        # что бит реально перевернётся.
        token = create_access_token("u", roles=[])
        parts = token.split(".")
        sig = parts[2]
        # Берём средний символ и меняем на другой.
        mid = len(sig) // 2
        replacement = "A" if sig[mid] != "A" else "B"
        parts[2] = sig[:mid] + replacement + sig[mid + 1:]
        bad = ".".join(parts)
        with pytest.raises(JWTError):
            decode_access_token(bad)

    def test_garbage_token_rejected(self):
        with pytest.raises(JWTError):
            decode_access_token("not.a.valid.jwt")


# ---------------------------------------------------------------------
# Random tokens
# ---------------------------------------------------------------------


class TestRandomTokens:
    def test_refresh_token_length_and_charset(self):
        t = create_refresh_token_value()
        # secrets.token_hex(32) → 64 hex-символа.
        assert len(t) == 64
        assert all(c in "0123456789abcdef" for c in t)

    def test_refresh_tokens_are_unique(self):
        # Достаточный случайностный пул — даже 100 токенов не дают
        # коллизий.
        tokens = {create_refresh_token_value() for _ in range(100)}
        assert len(tokens) == 100

    def test_hash_token_deterministic(self):
        # SHA-256 — детерминированный: одинаковый вход → одинаковый
        # выход. Иначе мы не сможем найти токен по хешу в БД.
        h1 = hash_token("abc")
        h2 = hash_token("abc")
        assert h1 == h2
        # SHA-256 даёт 64 hex-символа.
        assert len(h1) == 64

    def test_generate_verification_token_pair(self):
        raw, h = generate_verification_token()
        # Сам токен идёт юзеру в email; хеш — в БД.
        # Они НЕ должны совпадать — иначе хранение хеша теряет смысл.
        assert raw != h
        # И при повторном hash(raw) получаем тот же h (детерминизм).
        assert hash_token(raw) == h


# ---------------------------------------------------------------------
# Sanity: dummy_verify сравним по времени с реальной верификацией
# ---------------------------------------------------------------------


def test_dummy_verify_similar_time_to_real():
    """
    Защита от timing attack: dummy_verify должен занимать примерно
    столько же, сколько и реальная bcrypt-верификация. На bcrypt с
    cost=12 это десятки мс; точное равенство не требуется — лишь
    «той же ярмарки», в пределах 5x.
    """
    h = hash_password("test-pass")

    t0 = time.perf_counter()
    verify_password("test-pass", h)
    real = time.perf_counter() - t0

    t0 = time.perf_counter()
    dummy_verify_password()
    fake = time.perf_counter() - t0

    # 5x допуск: bcrypt-cost иногда вариативен на CI/dev-машинах.
    assert real / fake < 5
    assert fake / real < 5


# ---------------------------------------------------------------------
# BE-08: bcrypt не блокирует event loop
# ---------------------------------------------------------------------


async def test_async_password_helpers_do_not_block_event_loop():
    """Пока идёт bcrypt (~сотни мс CPU), loop обязан обслуживать другие
    корутины — иначе один логин замораживает весь API и WebSocket."""
    import asyncio

    from app.utils.security import (
        dummy_verify_password_async,
        hash_password_async,
        verify_password_async,
    )

    hashed = hash_password("Correct-Pass-1")
    ticks = 0
    stop = asyncio.Event()

    async def ticker():
        nonlocal ticks
        while not stop.is_set():
            ticks += 1
            await asyncio.sleep(0.001)

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0)
    before = ticks
    assert await verify_password_async("Correct-Pass-1", hashed) is True
    assert await verify_password_async("wrong", hashed) is False
    new_hash = await hash_password_async("Other-Pass-2")
    await dummy_verify_password_async()
    stop.set()
    await task

    assert verify_password("Other-Pass-2", new_hash)
    # За 4 операции bcrypt тикер должен был проснуться много раз.
    assert ticks - before > 10


# ---------------------------------------------------------------------
# BE-29: замена python-jose/passlib/bleach
# ---------------------------------------------------------------------

# Хэши, выпущенные прежней реализацией (passlib + bcrypt<4.1). Новая
# реализация обязана их проверять — иначе после выкатки никто не войдёт.
_LEGACY_HASH = "$2b$12$pqvVzSHrdMZrJ.Y4LqytZ.8AWEDa0opQHet0/4e6acDsu4MQXaf1."
# Пароль длиннее 72 байт: passlib молча обрезал его до 72 при хэшировании.
_LEGACY_LONG_HASH = "$2b$12$BSxrs7E0FgZDeblwEHtX2eboYnxYSAwG5JxE7i4hptuwjmWil9ug6"


def test_legacy_passlib_hash_still_verifies():
    assert verify_password("Legacy-Pass-1", _LEGACY_HASH)
    assert not verify_password("wrong", _LEGACY_HASH)


def test_legacy_long_password_verifies_without_error():
    assert verify_password("Ж" * 40, _LEGACY_LONG_HASH)


def test_deprecated_libraries_not_imported():
    import pathlib
    import re

    pattern = re.compile(r"^\s*(from|import)\s+(jose|passlib|bleach|structlog)\b", re.M)
    offenders = [
        str(p)
        for base in ("app", "worker", "scripts")
        for p in pathlib.Path(base).rglob("*.py")
        if pattern.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_jwt_alg_none_rejected():
    import base64
    import json

    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    token = b64({"alg": "none", "typ": "JWT"}) + "." + b64(
        {"sub": "x", "type": "access", "exp": int(time.time()) + 60}
    ) + "."
    with pytest.raises(JWTError):
        decode_access_token(token)
