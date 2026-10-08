"""
Оповещения о признаках атаки (план защиты 2026-10-05, этап 3).

Задача планировщика раз в минуту прогоняет правила по счётчикам из
app/services/security_metrics.py. Сработавшее правило отправляет
сообщение не чаще раза в `alert_cooldown_seconds` (SET NX в Redis) —
иначе во время атаки канал засыпало бы одинаковыми сообщениями.

Каналы (оба опциональны, включаются переменными окружения):
- Telegram: ALERT_TELEGRAM_BOT_TOKEN + ALERT_TELEGRAM_CHAT_ID;
- почта: ALERT_EMAIL (через тот же SMTP, что и остальные письма).
Без каналов оповещение пишется в лог app.security уровнем WARNING.

В тексте — только агрегированные числа: ни телефонов, ни email, ни IP.
Telegram — зарубежный сервис, персональные данные туда не уходят (152-ФЗ).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from redis.asyncio import Redis

from app.config import settings
from app.services import security_metrics as m
from app.services.email import send_email

logger = logging.getLogger(__name__)
security_logger = logging.getLogger("app.security")

_TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"


@dataclass
class Alert:
    rule: str
    text: str


async def _evaluate(redis: Redis, now: datetime) -> list[Alert]:
    found: list[Alert] = []

    limited = await m.total(redis, m.RATE_LIMITED, minutes=10, now=now)
    if limited >= settings.alert_rate_limited_10m:
        found.append(Alert(
            "rate_limited",
            f"Всплеск отказов по лимитам: {limited} ответов 429 за 10 минут. "
            "Возможна атака перебором или DoS.",
        ))

    errors = await m.total(redis, m.HTTP_5XX, minutes=10, now=now)
    if errors >= settings.alert_5xx_10m:
        found.append(Alert(
            "http_5xx",
            f"Ошибки сервера: {errors} ответов 5xx за 10 минут.",
        ))

    sent = await m.total(redis, m.SMS_SENT, minutes=60, now=now)
    verified = await m.total(redis, m.OTP_VERIFIED, minutes=60, now=now)
    if sent >= settings.alert_sms_min_sent_1h:
        conversion = verified / sent
        if conversion < settings.alert_sms_min_conversion:
            found.append(Alert(
                "sms_conversion",
                f"Подозрение на накрутку SMS: за час отправлено {sent} SMS, "
                f"код ввели только в {conversion:.0%} случаев. Проверьте "
                "расходы в кабинете SMS-шлюза; при необходимости снизьте "
                "SMS_DAILY_BUDGET.",
            ))

    budget = settings.sms_daily_budget
    if budget > 0:
        used = int(await redis.get(f"otp:budget:{now:%Y-%m-%d}") or 0)
        if used >= budget * settings.alert_sms_budget_share:
            found.append(Alert(
                "sms_budget",
                f"Суточный бюджет SMS израсходован на {used / budget:.0%} "
                f"({used} из {budget}).",
            ))

    locked = await m.total(redis, m.ACCOUNT_LOCKED, minutes=60, now=now)
    if locked >= settings.alert_account_locked_1h:
        found.append(Alert(
            "account_locked",
            f"За час заблокировано {locked} аккаунтов после серии неверных "
            "паролей. Возможен подбор паролей по базе утечек.",
        ))

    captcha = await m.total(redis, m.CAPTCHA_FAILED, minutes=10, now=now)
    if captcha >= settings.alert_captcha_failed_10m:
        found.append(Alert(
            "captcha_failed",
            f"{captcha} отклонённых решений капчи за 10 минут — "
            "кто-то пытается обойти проверку.",
        ))

    return found


async def deliver(text: str) -> None:
    """Отправить сообщение во все настроенные каналы. Ошибки — в лог."""
    message = f"Show Ring — {text}"
    security_logger.warning("security_alert %s", text)

    token, chat = settings.alert_telegram_bot_token, settings.alert_telegram_chat_id
    if token and chat:
        try:
            async with httpx.AsyncClient(timeout=10) as http:
                r = await http.post(
                    _TELEGRAM_URL.format(token=token),
                    json={"chat_id": chat, "text": message},
                )
                r.raise_for_status()
        except Exception:  # noqa: BLE001 — сбой канала не должен ронять задачу
            logger.exception("Не удалось отправить оповещение в Telegram")

    if settings.alert_email:
        try:
            await send_email(
                to_email=settings.alert_email,
                subject="Show Ring: оповещение безопасности",
                html_body=f"<p>{message}</p>",
                text_body=message,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Не удалось отправить оповещение на почту")


async def check_and_send(redis: Redis, *, now: datetime | None = None) -> list[str]:
    """Прогнать правила и отправить новые оповещения. Возвращает их id."""
    now = now or datetime.now(timezone.utc)
    fired: list[str] = []
    for alert in await _evaluate(redis, now):
        fresh = await redis.set(
            f"alert:sent:{alert.rule}",
            "1",
            nx=True,
            ex=settings.alert_cooldown_seconds,
        )
        if not fresh:
            continue
        await deliver(alert.text)
        fired.append(alert.rule)
    return fired
