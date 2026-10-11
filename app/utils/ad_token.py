"""
Токен показа рекламы (ревью 2026-10-06, BE-25).

/ads/serve выдаёт его вместе с баннером, /ads/events учитывает событие
только с валидным токеном этого баннера. Без токена показы и клики (и
списание бюджета чужой кампании) накручивались простыми запросами.

Формат: AD1.<base64url(banner_id 16 байт + issued_at 8 байт + nonce 8 байт)>.<base64url(HMAC[:16])>

- issued_at — Unix-время выдачи; токен живёт TTL_SECONDS.
- nonce — случайный: по нему роутер засчитывает токен один раз на тип
  события (Redis), повтор с другого IP/UA не учитывается.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
import uuid

from app.config import settings

VERSION = "AD1"
TTL_SECONDS = 30 * 60
_SIG_LEN = 16
_PAYLOAD_LEN = 16 + 8 + 8
_MAX_LEN = 200


class InvalidAdToken(ValueError):
    """Токен не разобран, подделан или просрочен."""


def _secret() -> bytes:
    # Доменное разделение: производный ключ ≠ ключ подписи JWT и токена чек-ина.
    return hmac.new(
        settings.secret_key.encode(), b"show-ring/ad-impression-token/v1", hashlib.sha256
    ).digest()


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    if _b64e(raw) != text:
        raise ValueError("non-canonical base64")
    return raw


def _sign(signed_part: str, key: bytes) -> bytes:
    return hmac.new(key, signed_part.encode("ascii"), hashlib.sha256).digest()[:_SIG_LEN]


def make_token(
    banner_id: uuid.UUID, *, issued_at: int | None = None, key: bytes | None = None
) -> str:
    ts = int(time.time()) if issued_at is None else issued_at
    payload = banner_id.bytes + struct.pack(">Q", ts) + secrets.token_bytes(8)
    signed_part = f"{VERSION}.{_b64e(payload)}"
    return f"{signed_part}.{_b64e(_sign(signed_part, key or _secret()))}"


def parse_token(token: str, *, now: int | None = None) -> tuple[uuid.UUID, str]:
    """Проверить подпись и срок; вернуть (banner_id, nonce в hex)."""
    if not isinstance(token, str) or not token or len(token) > _MAX_LEN:
        raise InvalidAdToken("malformed")
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != VERSION:
        raise InvalidAdToken("malformed")
    signed_part = f"{parts[0]}.{parts[1]}"
    try:
        payload = _b64d(parts[1])
        sig = _b64d(parts[2])
    except ValueError as e:
        raise InvalidAdToken("malformed") from e
    if len(payload) != _PAYLOAD_LEN:
        raise InvalidAdToken("malformed")
    if not hmac.compare_digest(sig, _sign(signed_part, _secret())):
        raise InvalidAdToken("bad_signature")
    (issued_at,) = struct.unpack(">Q", payload[16:24])
    current = int(time.time()) if now is None else now
    # Небольшой допуск на расхождение часов между инстансами.
    if issued_at > current + 60 or current - issued_at > TTL_SECONDS:
        raise InvalidAdToken("expired")
    return uuid.UUID(bytes=payload[:16]), payload[24:].hex()
