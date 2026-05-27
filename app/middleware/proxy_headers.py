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

Реализация: переписываем request.scope['client'] на (real_ip, port).
После middleware вся остальная цепочка видит правильный IP.
"""

from __future__ import annotations

import ipaddress
import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

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


class ProxyHeadersMiddleware(BaseHTTPMiddleware):
    """
    Подменяет client IP из X-Forwarded-For, если peer в списке
    доверенных прокси.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        if _TRUSTED_NETS:
            peer = request.client.host if request.client else None
            if _is_trusted_peer(peer):
                xff = request.headers.get("x-forwarded-for")
                if xff:
                    # X-Forwarded-For: "client, proxy1, proxy2".
                    # Клиент — первый в списке (leftmost).
                    real_ip = xff.split(",")[0].strip()
                    # ИСПРАВЛЕНО (bug_012 ultrareview): валидируем,
                    # что строка действительно IP. Без проверки:
                    # - empty XFF (nginx misconfig с пустым
                    #   $proxy_add_x_forwarded_for) → real_ip="" →
                    #   rate-limit (rate:{ip}:{ep}) и ad-fraud
                    #   dedup (ad_dedup:{banner}:{ip}:...) рушатся,
                    #   все анонимы в одной корзине;
                    # - nginx по умолчанию APPENDS XFF, не replaces;
                    #   client-controlled значение проходит как
                    #   leftmost token, давая rate-limit bypass
                    #   ротацией XFF на /auth/login.
                    # Симметрия с _is_trusted_peer, где такая же
                    # валидация уже есть.
                    try:
                        ipaddress.ip_address(real_ip)
                    except ValueError:
                        logger.warning(
                            "Trusted proxy %s sent malformed XFF %r",
                            peer, xff,
                        )
                    else:
                        # Подменяем scope. Порт сохраняем из исходного
                        # request.client (port в XFF не передаётся).
                        port = request.client.port if request.client else 0
                        request.scope["client"] = (real_ip, port)
                # X-Forwarded-Proto для корректного scheme в HTTPS-режиме
                # за reverse-proxy. Без этого request.url.scheme='http'
                # даже когда клиент пришёл по HTTPS.
                proto = request.headers.get("x-forwarded-proto")
                if proto in ("http", "https"):
                    request.scope["scheme"] = proto
        return await call_next(request)
