"""
Токен QR-билета чек-ина.

Формат: SR1.<base64url(show_id 16 байт + user_id 16 байт)>.<base64url(HMAC[:16])>

Принцип: токен ИДЕНТИФИЦИРУЕТ участника, но НЕ АВТОРИЗУЕТ действий.
Скан лишь ускоряет поиск на стойке; права даёт роль регистратора, а
допуск — проверка собаки на месте. Поэтому без срока действия и без
хранения в БД: подпись пересчитывается при каждом скане.

Почему не JWT: QR из ~70 символов — версия 4–5, читается бюджетной
камерой; JWT в 3–4 раза длиннее, а его claims здесь не нужны.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import uuid

from app.config import settings

VERSION = "SR1"
_SIG_LEN = 16
_MAX_LEN = 200
_VERSION_RE = re.compile(r"SR\d+")


class InvalidCheckinToken(ValueError):
    """reason: malformed | unsupported_version | bad_signature."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _secret() -> bytes:
    if settings.checkin_token_secret:
        return settings.checkin_token_secret.encode()
    # Доменное разделение: производный ключ ≠ ключ подписи JWT.
    return hmac.new(
        settings.secret_key.encode(), b"show-ring/checkin-token/v1", hashlib.sha256
    ).digest()


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    # Каноничность: urlsafe_b64decode молча игнорирует мусор и «лишние»
    # младшие биты последнего символа — без этой проверки два разных
    # токена давали бы одинаковые байты.
    if _b64e(raw) != text:
        raise ValueError("non-canonical base64")
    return raw


def _sign(signed_part: str, key: bytes) -> bytes:
    return hmac.new(key, signed_part.encode("ascii"), hashlib.sha256).digest()[:_SIG_LEN]


def make_token(
    show_id: uuid.UUID, user_id: uuid.UUID, *, key: bytes | None = None
) -> str:
    signed_part = f"{VERSION}.{_b64e(show_id.bytes + user_id.bytes)}"
    return f"{signed_part}.{_b64e(_sign(signed_part, key or _secret()))}"


def parse_token(
    token: str, *, key: bytes | None = None
) -> tuple[uuid.UUID, uuid.UUID]:
    if not isinstance(token, str) or not token or len(token) > _MAX_LEN:
        raise InvalidCheckinToken("malformed")
    parts = token.strip().split(".")
    if len(parts) != 3:
        raise InvalidCheckinToken("malformed")
    version, payload, sig = parts
    if version != VERSION:
        if _VERSION_RE.fullmatch(version):
            raise InvalidCheckinToken("unsupported_version")
        raise InvalidCheckinToken("malformed")
    try:
        raw = _b64d(payload)
        sig_raw = _b64d(sig)
    except (binascii.Error, ValueError):
        raise InvalidCheckinToken("malformed") from None
    if len(raw) != 32 or len(sig_raw) != _SIG_LEN:
        raise InvalidCheckinToken("malformed")
    expected = _sign(f"{version}.{payload}", key or _secret())
    if not hmac.compare_digest(expected, sig_raw):
        raise InvalidCheckinToken("bad_signature")
    return uuid.UUID(bytes=raw[:16]), uuid.UUID(bytes=raw[16:])
