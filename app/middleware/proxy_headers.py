"""
Доверие X-Forwarded-* только от известных прокси (этап 14 follow-up).

За nginx/cloudflare/load-balancer'ом `request.client.host` указывает на
ip самого прокси (127.0.0.1), а реальный IP клиента в X-Forwarded-For.
Это критично для:
- ad fraud (дедупликация по IP — иначе все клиенты выглядят одинаково),
- rate limiting (без правильного IP блокируем сам прокси),
- логов аудита.

НО: доверять X-Forwarded-For от ЛЮБОГО peer'а опасно — анонимный
клиент пришлёт `X-Forwarded-For: <чей_угодно_ip>` и обойдёт все
IP-based проверки. Поэтому доверяем заголовку ТОЛЬКО если запрос
пришёл с одного из IP в forwarded_allow_ips.

Реализация: переписываем scope['client'] на (real_ip, port) — и для HTTP,
и для WebSocket.
После middleware вся остальная цепочка видит правильный IP.
"""

from __future__ import annotations

import ipaddress
import logging

from starlette.datastructures import Headers

from app.config import settings

logger = logging.getLogger(__name__)


def _parse_networks(items: list[str]) -> list:
    """Превращает список CIDR/IP в IPv4Network/IPv6Network объекты."""
    nets = []
    for it in items:
        try:
            # ip_network допускает и одиночный IP ("10.0.0.1") — будет
            # /32 для v4 и /128 для v6, как нам и нужно.
            nets.append(ipaddress.ip_network(it.strip(), strict=False))
        except ValueError as e:
            logger.warning("forwarded_allow_ips: bad entry %r (%s)", it, e)
    return nets


# Парсим один раз при импорте — settings не меняется в рантайме.
_TRUSTED_NETS = _parse_networks(settings.forwarded_allow_ips)


def _is_trusted_peer(host: str | None) -> bool:
    if host is None or not _TRUSTED_NETS:
        return False
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(addr in net for net in _TRUSTED_NETS)


def _rightmost_untrusted(xff: str) -> str:
    """Самый правый адрес X-Forwarded-For, не принадлежащий нашим прокси.

    Если вся цепочка — доверенные адреса, возвращаем самый левый из них.
    """
    hops = [h.strip() for h in xff.split(",") if h.strip()]
    for hop in reversed(hops):
        if not _is_trusted_peer(hop):
            return hop
    return hops[0] if hops else ""


class ProxyHeadersMiddleware:
    """
    Подменяет client IP из X-Forwarded-For, если peer в списке
    доверенных прокси.

    Чистый ASGI, а не BaseHTTPMiddleware (ревью 2026-10-06, BE-01):
    BaseHTTPMiddleware пропускает websocket-scope без обработки, и за
    nginx все WS-клиенты выглядели одним IP — rate-limit хендшейка
    (ws_rate_limit) становился общим на весь сайт.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] in ("http", "websocket") and _TRUSTED_NETS:
            _apply_forwarded(scope)
        await self.app(scope, receive, send)


def _apply_forwarded(scope) -> None:
    client = scope.get("client")
    peer = client[0] if client else None
    if not _is_trusted_peer(peer):
        return
    headers = Headers(scope=scope)
    xff = headers.get("x-forwarded-for")
    if xff:
        # ИСПРАВЛЕНО (ревью безопасности 2026-10-03, #1): левую часть XFF
        # присылает сам клиент, прокси лишь дописывают справа. Идём справа
        # налево, пропуская наши доверенные прокси; первый недоверенный
        # адрес — клиент, каким его увидел крайний наш прокси.
        real_ip = _rightmost_untrusted(xff)
        # ИСПРАВЛЕНО (bug_012 ultrareview): строка обязана быть IP — иначе
        # пустой/битый XFF схлопывал всех анонимов в одну корзину
        # rate-limit'а и ad-dedup'а.
        try:
            ipaddress.ip_address(real_ip)
        except ValueError:
            logger.warning("Trusted proxy %s sent malformed XFF %r", peer, xff)
        else:
            # Порт в XFF не передаётся — сохраняем исходный.
            port = client[1] if client else 0
            scope["client"] = (real_ip, port)
    # X-Forwarded-Proto: за reverse-proxy без него scheme был бы http/ws
    # даже при HTTPS/WSS у клиента.
    proto = headers.get("x-forwarded-proto")
    if proto in ("http", "https"):
        if scope["type"] == "websocket":
            scope["scheme"] = "wss" if proto == "https" else "ws"
        else:
            scope["scheme"] = proto
