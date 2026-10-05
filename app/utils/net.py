"""Сетевые хелперы для лимитов по подсети."""

from __future__ import annotations

import ipaddress


def ip_subnet(ip: str) -> str:
    """
    Подсеть адреса для группового rate-limit: IPv4 → /24, IPv6 → /64.

    Пул прокси у атакующего обычно идёт пачками соседних адресов, а
    провайдер IPv6 выдаёт абоненту целую /64 — лимит «на IP» тут ничего
    не ограничивает. Нераспознанная строка (например, "unknown", когда
    request.client пуст) возвращается как есть.
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    prefix = 24 if addr.version == 4 else 64
    return str(ipaddress.ip_network(f"{addr}/{prefix}", strict=False))
