"""
Приём отчётов о нарушениях Content Security Policy (технический долг,
«CSP: стили» — эндпоинт report-uri).

Фронт (src/proxy.ts) ставит в CSP директиву `report-uri /api/csp-report`:
браузер сам присылает сюда POST, когда политика что-то заблокировала (в
режиме enforce) или заблокировала бы (report-only). Без этого в проде не
узнать, что строгая политика сломала страницу у пользователей.

Что пишется: в лог app.security — директива, источник заблокированного
ресурса (только схема и хост) и путь страницы БЕЗ query (в query бывают
токены из писем); в метрики — счётчик csp_violation. Тело отчёта целиком не
сохраняется: в нём может оказаться что угодно со страницы.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, Response, status

from app import redis as redis_state
from app.middleware.progressive_ban import check_rate_limit
from app.services import security_metrics

router = APIRouter(tags=["security"])
logger = logging.getLogger("app.security")

# Отчёт — пара сотен байт; больше — не отчёт браузера.
_MAX_BODY_BYTES = 16 * 1024
# Одна страница со сломанным ресурсом шлёт по отчёту на каждый блок —
# 60 в минуту с IP с запасом, дальше лимит защищает лог от флуда.
_RATE_LIMIT = 60

# Значения blocked-uri без URL (inline-скрипт, eval и т.п.) — как есть.
_KEYWORDS = {"inline", "eval", "self", "data", "blob", "wasm-eval", "trusted-types-policy"}


def _origin(value: Any) -> str:
    """Источник ресурса без пути и query: https://cdn.example.com."""
    if not isinstance(value, str) or not value:
        return "-"
    if value in _KEYWORDS:
        return value
    parts = urlsplit(value)
    if parts.scheme in ("data", "blob"):
        return parts.scheme
    if parts.scheme and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return value[:32]


def _path(value: Any) -> str:
    """Путь страницы без query и fragment."""
    if not isinstance(value, str) or not value:
        return "-"
    return urlsplit(value).path[:200] or "/"


def _short(value: Any, limit: int = 64) -> str:
    return value[:limit] if isinstance(value, str) and value else "-"


def _violations(payload: Any) -> list[dict]:
    """
    Отчёты обоих форматов:
    - report-uri: {"csp-report": {"violated-directive": …, "blocked-uri": …}};
    - Reporting API: [{"type": "csp-violation", "body": {"effectiveDirective": …}}].
    Приводим к одному виду.
    """
    out: list[dict] = []
    if isinstance(payload, dict) and isinstance(payload.get("csp-report"), dict):
        r = payload["csp-report"]
        out.append({
            "directive": r.get("effective-directive") or r.get("violated-directive"),
            "blocked": r.get("blocked-uri"),
            "page": r.get("document-uri"),
            "disposition": r.get("disposition"),
        })
    elif isinstance(payload, list):
        for item in payload[:20]:
            if not isinstance(item, dict) or item.get("type") != "csp-violation":
                continue
            body = item.get("body")
            if not isinstance(body, dict):
                continue
            out.append({
                "directive": body.get("effectiveDirective"),
                "blocked": body.get("blockedURL"),
                "page": body.get("documentURL") or item.get("url"),
                "disposition": body.get("disposition"),
            })
    return out


@router.post(
    "/csp-report",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Отчёт браузера о нарушении CSP",
    include_in_schema=False,
)
async def csp_report(request: Request) -> Response:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > _MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Отчёт слишком большой")

    client = redis_state.redis_client
    if client is not None:
        await check_rate_limit(request, limit=_RATE_LIMIT, window=60, redis=client)

    raw = await request.body()
    if len(raw) > _MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Отчёт слишком большой")
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        # Мусор браузеру не вернёшь — отвечаем как на принятый отчёт.
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    violations = _violations(payload)
    for v in violations:
        logger.warning(
            "csp_violation directive=%s blocked=%s page=%s disposition=%s",
            _short(v["directive"]),
            _origin(v["blocked"]),
            _path(v["page"]),
            _short(v["disposition"], 16),
        )
    if violations:
        await security_metrics.record(security_metrics.CSP_VIOLATION, len(violations))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
