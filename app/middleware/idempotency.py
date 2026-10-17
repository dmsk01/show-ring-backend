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

Сбой Redis по умолчанию — fail-open: запрос выполняется без защиты от
повтора (лучше, чем отказать во всём API). Для путей из
settings.idempotency_required_paths — fail-closed: 503, клиент повторит
позже с тем же ключом. Двойная оплата хуже, чем «попробуйте ещё раз».

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

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.config import settings
from app.middleware.asgi_body import ClientDisconnected, read_body, replay_receive

# Импортируем МОДУЛЬ, а не значение: init_redis() пере-присваивает
# app.redis.redis_client уже после импорта этого файла. Value-импорт
# (`from app.redis import redis_client`) связал бы имя с None навсегда —
# и весь Idempotency-механизм молча отключился бы (ровно это и было,
# см. review 2026-06-10; тот же баг ранее — в ws_manager).
from app import redis as redis_state

logger = logging.getLogger(__name__)


_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_HEADER = "Idempotency-Key"
# In-flight lock TTL: разумный потолок одного HTTP-запроса. Если запрос
# завис на больше 60 секунд — что-то пошло не так в любом случае. После
# успешной/ошибочной обработки lock удаляется явно (см. finally ниже),
# TTL — страховка от утечки lock'а при крэше процесса.
_IN_FLIGHT_TTL_SECONDS = 60

# ИСПРАВЛЕНО (review 2026-05-28): лимит размера кэшируемого тела.
# Раньше middleware безусловно буферизовал ВЕСЬ 2xx-ответ в Redis. Для
# эндпоинтов, возвращающих большие JSON (агрегации, batch-операции) или
# бинарь (если когда-нибудь PDF будут отдавать через POST), это удваивало
# память на ответ и держало копию в Redis сутки. 256 KB покрывает
# подавляющее большинство JSON-ответов API; для больших ответов
# семантика остаётся — клиент получает результат, in-flight lock
# защищает от двойной обработки, — но кэш просто не пишется, и второй
# идентичный запрос пройдёт заново через handler.
_MAX_CACHED_BODY_BYTES = 256 * 1024

# Заголовки, которые нельзя воспроизводить из кэша (BE-09): Set-Cookie
# переотправил бы чужие/старые куки (сессию), X-Request-ID — id чужого
# запроса, длину/дату Starlette выставит сам.
_UNCACHEABLE_HEADERS = frozenset(
    {"set-cookie", "x-request-id", "content-length", "date", "server"}
)

# Не кэшируем auth: ответ несёт токены в куках/теле, а повтор логина из
# кэша выдал бы их без проверки пароля.
_EXCLUDED_PATH_PREFIXES = ("/auth/",)

# Тело запроса читается в память целиком (для хэша) — большие тела
# (загрузки файлов) пропускаем без idempotency.
_MAX_REQUEST_BODY_BYTES = 1024 * 1024


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
    # ИСПРАВЛЕНО (ревью 2026-10-06, BE-09): веб-клиент авторизуется
    # httpOnly-кукой access_token без заголовка Authorization — раньше для
    # всего веба identity была IP, и пользователи за одним NAT делили кэш.
    token = request.headers.get("authorization") or request.cookies.get(
        "access_token"
    )
    if token:
        return "u:" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:32]
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


def _fail_closed(path: str) -> bool:
    """Путь из idempotency_required_paths: без Redis не выполнять."""
    return any(path.startswith(p) for p in settings.idempotency_required_paths)


def _unavailable() -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"detail": "Сервис временно недоступен, повторите запрос позже"},
        headers={"Retry-After": "5"},
    )


def _replay(snapshot: dict) -> Response:
    """
    Ответ из кэша. headers — список пар [имя, значение] (повторяющиеся
    заголовки сохраняются); снимки, записанные до BE-28, хранили словарь —
    они живут в Redis до суток, поэтому читаем оба формата.
    """
    response = Response(
        content=snapshot["body"].encode(snapshot["encoding"]),
        status_code=snapshot["status"],
        media_type=snapshot.get("media_type"),
    )
    headers = snapshot["headers"]
    pairs = headers.items() if isinstance(headers, dict) else headers
    cached = [
        (k.lower().encode("latin-1"), v.encode("latin-1"))
        for k, v in pairs
        if k.lower() not in _UNCACHEABLE_HEADERS
    ]
    names = {k for k, _ in cached}
    # Content-Length — от тела, которое реально отдаём; Content-Type из
    # снимка (если был) важнее дефолта Response.
    own = [(k, v) for k, v in response.raw_headers if k not in names]
    response.raw_headers = own + cached
    return response


class IdempotencyMiddleware:
    """
    Чистый ASGI (ревью 2026-10-06, BE-28). Раньше — BaseHTTPMiddleware: тело
    подменялось через приватные поля Request, а ответ буферизовался целиком
    и пересобирался заново. Теперь тело читается из receive и отдаётся
    приложению своим receive, а ответ уходит клиенту как есть — копия для
    кэша собирается параллельно (до _MAX_CACHED_BODY_BYTES).
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope["method"] not in _UNSAFE_METHODS:
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        # Читаем клиента через модуль (см. комментарий у импорта) и
        # фиксируем в локальную переменную — единый снимок на весь запрос.
        redis_client = redis_state.redis_client
        key = request.headers.get(_HEADER)
        path = scope["path"]
        if not key or path.startswith(_EXCLUDED_PATH_PREFIXES):
            await self.app(scope, receive, send)
            return
        fail_closed = _fail_closed(path)
        if redis_client is None:
            if fail_closed:
                logger.warning("idempotency fail-closed: Redis client missing")
                await _unavailable()(scope, receive, send)
                return
            await self.app(scope, receive, send)
            return
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > _MAX_REQUEST_BODY_BYTES:
            await self.app(scope, receive, send)
            return

        try:
            body = await read_body(receive)
        except ClientDisconnected:
            return
        app_receive = replay_receive(body, receive)
        body_hash = hashlib.sha256(body).hexdigest()
        identity = _caller_identity(request)
        cache_key = _cache_key(scope["method"], path, key, body_hash, identity)

        # 1. Пробуем достать готовый ответ.
        try:
            cached = await redis_client.get(cache_key)
        except Exception as e:  # noqa: BLE001 — fail-open при сбое Redis
            logger.warning("idempotency cache GET failed: %s", e)
            if fail_closed:
                await _unavailable()(scope, receive, send)
                return
            cached = None

        if cached is not None:
            try:
                replay = _replay(json.loads(cached))
            except (ValueError, KeyError, TypeError, AttributeError) as e:
                # Битый кэш — пропускаем мимо, обработаем заново.
                logger.warning("idempotency cache corrupt: %s", e)
            else:
                logger.info("idempotency hit", extra={"key": key, "path": path})
                await replay(scope, receive, send)
                return

        # 2. Кэша нет — проверяем in-flight lock. SETNX гарантирует
        # атомарность: только ОДИН одновременный запрос с таким ключом
        # пройдёт дальше, остальные получат 409 (Conflict).
        lock_key = _lock_key(scope["method"], path, key, body_hash, identity)
        try:
            acquired = await redis_client.set(
                lock_key, "1", nx=True, ex=_IN_FLIGHT_TTL_SECONDS
            )
        except Exception as e:  # noqa: BLE001 — fail-open
            logger.warning("idempotency lock SETNX failed: %s", e)
            if fail_closed:
                await _unavailable()(scope, receive, send)
                return
            acquired = True  # без Redis работаем как раньше

        if not acquired:
            logger.info(
                "idempotency in-flight conflict", extra={"key": key, "path": path}
            )
            conflict = JSONResponse(
                status_code=409,
                content={
                    "detail": "Request with this Idempotency-Key is already in progress",
                },
            )
            await conflict(scope, receive, send)
            return

        # 3. Lock взят — выполняем запрос, параллельно копируя ответ.
        status_code = 0
        headers: list[list[str]] = []
        chunks: list[bytes] = []
        size = 0
        oversized = False

        async def capture_send(message) -> None:
            nonlocal status_code, size, oversized
            if message["type"] == "http.response.start":
                status_code = message["status"]
                for k, v in message.get("headers", []):
                    name = k.decode("latin-1")
                    if name.lower() not in _UNCACHEABLE_HEADERS:
                        headers.append([name, v.decode("latin-1")])
            elif message["type"] == "http.response.body" and not oversized:
                chunk = message.get("body", b"")
                size += len(chunk)
                if size > _MAX_CACHED_BODY_BYTES:
                    # Большой ответ клиент получает как есть, но в Redis его
                    # не пишем (см. обоснование на константе).
                    oversized = True
                    chunks.clear()
                else:
                    chunks.append(chunk)
            await send(message)

        try:
            await self.app(scope, app_receive, capture_send)
        except Exception:
            # Lock освобождаем сразу, чтобы повторная попытка клиента не
            # упёрлась в «409 уже выполняется», пока не истёк TTL.
            await _release(redis_client, lock_key, "on error")
            raise

        # Кэшируем ТОЛЬКО успешные ответы 2xx — иначе при 400 закэшируем
        # ошибку, и клиент не сможет переотправить корректный запрос.
        if 200 <= status_code < 300:
            if oversized:
                logger.info(
                    "idempotency cache skip: body > limit %d", _MAX_CACHED_BODY_BYTES
                )
            else:
                snapshot = {
                    "body": b"".join(chunks).decode("utf-8", errors="replace"),
                    "encoding": "utf-8",
                    "status": status_code,
                    "headers": headers,
                }
                try:
                    await redis_client.set(
                        cache_key,
                        json.dumps(snapshot),
                        ex=settings.idempotency_ttl_seconds,
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning("idempotency cache SET failed: %s", e)
        # Lock освобождаем ПОСЛЕ записи в кэш: иначе параллельный запрос в
        # окне «lock снят, кэш ещё не записан» выполнился бы повторно.
        await _release(redis_client, lock_key, f"status {status_code}")


async def _release(redis_client, lock_key: str, reason: str) -> None:
    try:
        await redis_client.delete(lock_key)
    except Exception as e:  # noqa: BLE001
        logger.warning("idempotency lock DEL (%s) failed: %s", reason, e)
