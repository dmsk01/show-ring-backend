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
        if data.get("status") != "OK":
            logger.error("sms.ru rejected: %s", data)
            raise SMSDeliveryError(
                f"sms.ru status_code={data.get('status_code')}"
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
                # Mock в проде = коды уходят только в лог, вход по
                # телефону фактически не работает. Громко предупреждаем.
                logger.warning(
                    "SMS_PROVIDER=mock при DEBUG=False — SMS не отправляются"
                )
            _provider = MockSMSProvider()
    return _provider
