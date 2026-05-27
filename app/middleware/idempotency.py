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

In-flight lock (SETNX) защищает от ДВОЙНОЙ обработки при параллельных
запросах с одним ключом: первый запрос ставит ключ idem:lock:..., второй
получает 409 (Conflict). Без lock'а оба запроса пошли бы в handler и
выполнили операцию дважды до того, как ответ-кэш заполнится. См.
блок acquired/lock_key ниже.
"""

from __future__ import annotations

import hashlib
import json
import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.config import settings
from app.redis import redis_client

logger = logging.getLogger(__name__)


_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_HEADER = "Idempotency-Key"
# In-flight lock TTL: разумный потолок одного HTTP-запроса. Если запрос
# завис на больше 60 секунд — что-то пошло не так в любом случае. После
# успешной/ошибочной обработки lock удаляется явно (см. finally ниже),
# TTL — страховка от утечки lock'а при крэше процесса.
_IN_FLIGHT_TTL_SECONDS = 60


def _caller_identity(request: Request) -> str:
    """
    Идентификатор вызывающего для namespace'а idempotency-ключей.
    ИСПРАВЛЕНО (bug_018 ultrareview): без identity user B мог получить
    cached-ответ user A (cache-hit короткозамыкает middleware до
    Depends(get_current_user)), плюс auth bypass для replay
    перехваченного ключа+тела.

    Реализация:
    - Если есть Authorization header — sha256(header). Хешируем,
      чтобы JWT не лежал в Redis-ключах открытым (Redis MONITOR /
      RDB-снапшоты не должны содержать токены).
    - Анонимы — client IP. ProxyHeadersMiddleware (bug_012-фикс)
      гарантирует, что IP корректный и не подделан.

    Долгосрочно правильнее перенести Idempotency-логику в Depends
    ПОСЛЕ get_current_user — там identity явно authenticated. Сейчас
    минимальный fix в рамках middleware-архитектуры.
    """
    auth = request.headers.get("authorization")
    if auth:
        return "u:" + hashlib.sha256(auth.encode("utf-8")).hexdigest()[:32]
    if request.client is not None:
        return "ip:" + request.client.host
    return "anon"


def _cache_key(
    method: str, path: str, key: str, body_hash: str, identity: str
) -> str:
    """
    Полный кэш-ключ включает identity, method, path и body_hash:
    - identity — иначе разные пользователи делили бы кэш (см. bug_018);
    - method+path — иначе клиент мог бы переиспользовать ключ для
      разной операции и получить чужой ответ;
    - body_hash — защищает от случая «тот же ключ, но другое тело»:
      ответ из кэша не подходит, лучше пропустить мимо.
    """
    return f"idem:{identity}:{method}:{path}:{key}:{body_hash}"


def _lock_key(
    method: str, path: str, key: str, body_hash: str, identity: str
) -> str:
    """Ключ in-flight-локки, отдельно от кэша ответа; та же namespacing."""
    return f"idem:lock:{identity}:{method}:{path}:{key}:{body_hash}"


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
        identity = _caller_identity(request)
        cache_key = _cache_key(
            request.method, request.url.path, key, body_hash, identity
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

        # 2. Кэша нет — проверяем in-flight lock. SETNX гарантирует
        # атомарность: только ОДИН одновременный запрос с таким ключом
        # пройдёт дальше, остальные получат 409 (Conflict).
        # Без lock'а оба параллельных запроса дошли бы до handler'а
        # и сделали бы операцию дважды.
        lock_key = _lock_key(
            request.method, request.url.path, key, body_hash, identity
        )
        try:
            acquired = await redis_client.set(
                lock_key, "1", nx=True, ex=_IN_FLIGHT_TTL_SECONDS
            )
        except Exception as e:  # noqa: BLE001 — fail-open
            logger.warning("idempotency lock SETNX failed: %s", e)
            acquired = True  # без Redis работаем как раньше

        if not acquired:
            logger.info(
                "idempotency in-flight conflict",
                extra={"key": key, "path": request.url.path},
            )
            return JSONResponse(
                status_code=409,
                content={
                    "detail": "Request with this Idempotency-Key is already in progress",
                },
            )

        # 3. Lock взят — выполняем запрос и сохраняем ответ.
        # ИСПРАВЛЕНО (bug_001 ultrareview): подмены только `_receive`
        # недостаточно — Starlette `_CachedRequest.wrapped_receive`
        # возвращает кэшированный `_body` ДО обращения к `_receive`.
        # В idempotency body не меняется (читаем — и оригинальное тело
        # должно дойти до handler'а), но `_body` уже заполнен через
        # `await request.body()` выше — фактически дубль присваивания.
        # Закрепляем явно ради консистентности с sanitization.py и
        # как защиту от регрессии при будущей правке.
        request._body = body  # type: ignore[attr-defined]

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        # Тип параметра receive в Starlette шире, чем наш callback —
        # подменяем напрямую.
        request._receive = receive  # type: ignore[attr-defined]

        try:
            response = await call_next(request)
        except Exception:
            # На исключении lock освобождаем сразу, чтобы повторная попытка
            # клиента не упёрлась в "409 уже выполняется" пока TTL не истёк.
            try:
                await redis_client.delete(lock_key)
            except Exception as e:  # noqa: BLE001
                logger.warning("idempotency lock DEL on error failed: %s", e)
            raise

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

            # Lock освобождаем ПОСЛЕ записи в кэш. Если бы делали наоборот,
            # параллельный запрос (попавший в окно "lock освобождён, кэш
            # ещё не записан") пошёл бы выполняться вторично.
            try:
                await redis_client.delete(lock_key)
            except Exception as e:  # noqa: BLE001
                logger.warning("idempotency lock DEL failed: %s", e)

            # Возвращаем новый Response с уже прочитанным телом —
            # старый body_iterator исчерпан.
            return Response(
                content=full_body,
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type=response.media_type,
            )

        # Неуспешный ответ не кэшируем, но lock освобождаем — клиент
        # должен иметь возможность повторить запрос корректно.
        try:
            await redis_client.delete(lock_key)
        except Exception as e:  # noqa: BLE001
            logger.warning("idempotency lock DEL (non-2xx) failed: %s", e)
        return response
