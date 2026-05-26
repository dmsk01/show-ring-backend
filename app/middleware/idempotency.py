"""
Idempotency-Key middleware (этап 14).

Если клиент шлёт POST/PUT/PATCH/DELETE с заголовком Idempotency-Key,
повторный запрос с тем же ключом и таким же телом возвращает
закэшированный ответ. Защита от:
- двойного клика (network retry → задвоение операции),
- мобильных клиентов, которые ретраят запрос после плохого соединения.

Хранилище — Redis, TTL = settings.idempotency_ttl_seconds (24ч по
умолчанию).

Семантика следует RFC спецификации Idempotency-Key:
- ключ имеет смысл только для unsafe-методов (POST/PUT/PATCH/DELETE);
- хранится связка (метод + путь + ключ + хэш тела) — иначе клиент
  мог бы прислать тот же ключ для разных операций;
- TTL обязателен — без него Redis распух бы вечно.

Что НЕ сделано (TODO для prod):
- блокировка одновременных запросов с одним ключом (двойная отправка
  до завершения первой обработки). Сейчас оба запроса пойдут до Redis
  одновременно и оба запишут — последний выиграет. Решение через
  Redis SETNX-локку для in-flight.
"""

from __future__ import annotations

import hashlib
import json
import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.config import settings
from app.redis import redis_client

logger = logging.getLogger(__name__)


_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_HEADER = "Idempotency-Key"


def _cache_key(method: str, path: str, key: str, body_hash: str) -> str:
    """
    Полный кэш-ключ включает method+path — иначе клиент мог бы
    «переиспользовать» ключ для разной операции и получить чужой ответ.
    body_hash защищает от случая «тот же ключ, но другое тело» —
    тогда ответ из кэша некорректен, лучше пропустить мимо.
    """
    return f"idem:{method}:{path}:{key}:{body_hash}"


class IdempotencyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        if request.method not in _UNSAFE_METHODS:
            return await call_next(request)

        key = request.headers.get(_HEADER)
        if not key or redis_client is None:
            return await call_next(request)

        # Читаем тело и кладём обратно для нижестоящего обработчика —
        # body() в Starlette кэширует, но мы пересоздаём request.scope
        # на всякий случай: для POST'ов с большими телами повторное
        # чтение возможно.
        body = await request.body()
        body_hash = hashlib.sha256(body).hexdigest()
        cache_key = _cache_key(
            request.method, request.url.path, key, body_hash
        )

        # 1. Пробуем достать готовый ответ.
        try:
            cached = await redis_client.get(cache_key)
        except Exception as e:  # noqa: BLE001 — fail-open при сбое Redis
            logger.warning("idempotency cache GET failed: %s", e)
            cached = None

        if cached is not None:
            try:
                snapshot = json.loads(cached)
                logger.info(
                    "idempotency hit", extra={"key": key, "path": request.url.path}
                )
                return Response(
                    content=snapshot["body"].encode(snapshot["encoding"]),
                    status_code=snapshot["status"],
                    headers=snapshot["headers"],
                    media_type=snapshot.get("media_type"),
                )
            except (ValueError, KeyError) as e:
                # Битый кэш — пропускаем мимо, обработаем заново.
                logger.warning("idempotency cache corrupt: %s", e)

        # 2. Кэша нет — выполняем запрос и сохраняем ответ.
        # Body нужно перечитать downstream'ом; receive override
        # позволяет это сделать.
        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        # Тип параметра receive в Starlette шире, чем наш callback —
        # подменяем напрямую.
        request._receive = receive  # type: ignore[attr-defined]

        response = await call_next(request)

        # Кэшируем ТОЛЬКО успешные ответы 2xx — иначе при 400 закэшируем
        # ошибку, и клиент не сможет переотправить корректный запрос.
        if 200 <= response.status_code < 300:
            # body_iterator есть на StreamingResponse (FastAPI оборачивает
            # любой handler-ответ в него), но pyright видит общий тип
            # Response — берём через getattr с safe-fallback.
            body_iterator = getattr(response, "body_iterator", None)
            body_chunks: list[bytes] = []
            if body_iterator is not None:
                async for chunk in body_iterator:
                    body_chunks.append(chunk)
            full_body = b"".join(body_chunks)

            snapshot = {
                "body": full_body.decode("utf-8", errors="replace"),
                "encoding": "utf-8",
                "status": response.status_code,
                "headers": dict(response.headers),
                "media_type": response.media_type,
            }
            try:
                await redis_client.set(
                    cache_key,
                    json.dumps(snapshot),
                    ex=settings.idempotency_ttl_seconds,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("idempotency cache SET failed: %s", e)

            # Возвращаем новый Response с уже прочитанным телом —
            # старый body_iterator исчерпан.
            return Response(
                content=full_body,
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type=response.media_type,
            )

        return response
