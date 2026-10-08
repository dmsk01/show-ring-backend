"""
Слой интеграции с SMS-провайдерами.

Бизнес-логика (otp_auth) зависит только от абстракции SMSProvider и
получает реализацию через Depends(get_sms_provider) — подмена провайдера
(dev-mock, sms.ru, другой оператор) не трогает сервисы и роутеры.
"""

import logging
from abc import ABC, abstractmethod

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class SMSDeliveryError(Exception):
    """Провайдер не смог отправить SMS (сеть, баланс, ошибка API)."""


class SMSProvider(ABC):
    @abstractmethod
    async def send(self, phone: str, message: str) -> None:
        """Отправить SMS. Бросает SMSDeliveryError при сбое."""


class MockSMSProvider(SMSProvider):
    """Dev-провайдер: пишет сообщение в лог вместо реальной отправки."""

    async def send(self, phone: str, message: str) -> None:
        logger.info("[MOCK SMS] to=%s text=%r", phone, message)


class SmsRuProvider(SMSProvider):
    """
    sms.ru как пример реального провайдера (HTTP API).

    transport прокидывается для тестов (httpx.MockTransport); в проде
    остаётся None — httpx использует обычную сеть.
    """

    _URL = "https://sms.ru/sms/send"

    def __init__(
        self,
        api_key: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._transport = transport

    async def send(self, phone: str, message: str) -> None:
        try:
            async with httpx.AsyncClient(
                timeout=10, transport=self._transport
            ) as http:
                resp = await http.post(
                    self._URL,
                    data={
                        "api_id": self._api_key,
                        "to": phone,
                        "msg": message,
                        "json": 1,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as e:
            # Текст ошибки не отдаём клиенту (роутер вернёт 502) —
            # детали только в лог.
            logger.error("sms.ru request failed: %s", e)
            raise SMSDeliveryError("sms.ru request failed") from e
        except ValueError as e:
            # Не-JSON ответ (страница ошибки шлюза) — раньше уходил в 500.
            logger.error("sms.ru returned non-JSON response")
            raise SMSDeliveryError("sms.ru invalid response") from e
        if data.get("status") != "OK":
            logger.error("sms.ru rejected: %s", data)
            raise SMSDeliveryError(
                f"sms.ru status_code={data.get('status_code')}"
            )
        # Общий status=OK ещё не значит, что SMS ушло на этот номер: у sms.ru
        # статус по каждому номеру отдельный (ревью 2026-10-06, BE-38).
        for phone_status in (data.get("sms") or {}).values():
            if isinstance(phone_status, dict) and phone_status.get("status") != "OK":
                logger.error(
                    "sms.ru rejected phone: status_code=%s",
                    phone_status.get("status_code"),
                )
                raise SMSDeliveryError(
                    f"sms.ru status_code={phone_status.get('status_code')}"
                )


# Singleton: провайдер не хранит состояние запроса, создавать на каждый
# Depends незачем.
_provider: SMSProvider | None = None


def get_sms_provider() -> SMSProvider:
    """FastAPI-dependency: реализация по settings.sms_provider."""
    global _provider
    if _provider is None:
        if settings.sms_provider == "smsru":
            if not settings.sms_api_key:
                raise RuntimeError(
                    "SMS_API_KEY обязателен при SMS_PROVIDER=smsru"
                )
            _provider = SmsRuProvider(settings.sms_api_key)
        else:
            if not settings.debug:
                # ИСПРАВЛЕНО (ревью безопасности 2026-10-03, #2): раньше
                # здесь был warning. Mock пишет текст SMS с кодом в лог —
                # в проде любой с доступом к логам вошёл бы под чужим
                # номером. Отказываем: send-code вернёт 500, а не тихо
                # «отправит» код в лог.
                raise RuntimeError(
                    "SMS_PROVIDER=mock запрещён при DEBUG=False: "
                    "задайте SMS_PROVIDER=smsru и SMS_API_KEY"
                )
            _provider = MockSMSProvider()
    return _provider
