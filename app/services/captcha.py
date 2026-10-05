"""
Капча ALTCHA: proof-of-work, полностью на нашем сервере.

Как работает: сервер выдаёт подписанную задачу (GET /captcha/challenge),
браузер перебирает счётчик, пока PBKDF2 от него не даст ключ с нужным
префиксом (~0,5 с на устройстве), и присылает решение вместе с формой.
Для человека это незаметно, а для бота, шлющего тысячи запросов, —
тысячи секунд CPU. Сторонних сервисов и cookie нет, поэтому нет и
передачи данных третьим лицам (152-ФЗ).

Проверка:
- подпись задачи (HMAC) — нельзя подсунуть свою, более лёгкую задачу;
- подпись ключа (hmac_key_secret) — решение проверяется без повторного
  вычисления PBKDF2, за доли миллисекунды;
- срок действия задачи;
- одноразовость: подпись решённой задачи запоминается в Redis до её
  истечения, повторно тот же payload не принимается.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time

from altcha.v2 import Payload, create_challenge, verify_solution
from fastapi import HTTPException
from redis.asyncio import Redis

from app.config import settings

logger = logging.getLogger(__name__)
security_logger = logging.getLogger("app.security")

ALGORITHM = "PBKDF2/SHA-256"


def _derived_secret(purpose: str) -> bytes:
    # Отдельный секрет капче не заводим: производный от SECRET_KEY ключ
    # под конкретную цель. Утечка подписи капчи не раскрывает SECRET_KEY.
    return hmac.new(
        settings.secret_key.encode(), f"altcha:{purpose}".encode(), hashlib.sha256
    ).digest()


def new_challenge() -> dict:
    """Подписанная задача для виджета (формат ALTCHA v3)."""
    challenge = create_challenge(
        ALGORITHM,
        settings.captcha_cost,
        expires_at=int(time.time()) + settings.captcha_ttl_seconds,
        hmac_secret=_derived_secret("challenge"),
        hmac_key_secret=_derived_secret("key"),
    )
    return challenge.to_dict()


def _used_key(signature: str) -> str:
    return f"captcha:used:{signature}"


async def require_captcha(redis: Redis, payload: str | None) -> None:
    """
    Проверить решение капчи. 400 captcha_required — решения нет,
    400 captcha_invalid — неверное, просроченное или уже использованное.
    При captcha_enabled=False (тесты, локальная отладка) — пропуск.
    """
    if not settings.captcha_enabled:
        return
    if not payload:
        raise HTTPException(status_code=400, detail="captcha_required")

    try:
        parsed = Payload.from_base64(payload)
    except (ValueError, KeyError, TypeError, RecursionError):
        raise HTTPException(status_code=400, detail="captcha_invalid")

    result = verify_solution(
        parsed,
        _derived_secret("challenge"),
        hmac_key_secret=_derived_secret("key"),
    )
    signature = parsed.challenge.signature
    if not result.verified or not signature:
        security_logger.info(
            "captcha_rejected expired=%s bad_signature=%s bad_solution=%s",
            result.expired,
            result.invalid_signature,
            result.invalid_solution,
        )
        raise HTTPException(status_code=400, detail="captcha_invalid")

    # Одноразовость: SET NX атомарен — из двух параллельных запросов с одним
    # решением пройдёт ровно один. Ключ живёт не дольше самой задачи.
    first_use = await redis.set(
        _used_key(signature), "1", nx=True, ex=settings.captcha_ttl_seconds
    )
    if not first_use:
        security_logger.warning("captcha_replay")
        raise HTTPException(status_code=400, detail="captcha_invalid")
