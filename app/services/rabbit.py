import logging

import aio_pika

from app.services.rabbit_dlx import declare_workflow_queue

logger = logging.getLogger(__name__)


class RabbitMQService:
    def __init__(self):
        # RobustConnection — выживает разрывы сети, переподключается
        # автоматически. Тип Connection в аннотации — общий протокол;
        # фактически это RobustConnection после connect_robust().
        self.connection: aio_pika.abc.AbstractRobustConnection | None = None
        self.channel: aio_pika.abc.AbstractRobustChannel | None = None

    async def connect(self, url: str):
        # connect_robust возвращает RobustConnection. Параметры:
        # timeout=5 — не висеть на dead-сервере при старте API;
        # reconnect_interval=5 (default) — пауза между попытками.
        self.connection = await aio_pika.connect_robust(url, timeout=5)
        # Reconnect callbacks: aio-pika зовёт их при потере и восстановлении
        # соединения. Логируем — это нужно для post-mortem и алертинга.
        self.connection.reconnect_callbacks.add(self._on_reconnect)
        self.connection.close_callbacks.add(self._on_close)
        self.channel = await self.connection.channel()  # type: ignore[assignment]

    @staticmethod
    def _on_reconnect(
        connection: aio_pika.abc.AbstractRobustConnection | None,
    ) -> None:
        # Сигнатура с Optional — aio-pika type stubs допускают вызов
        # с None при инициализации/закрытии. Сам callback'у это
        # неважно — connection используется только в логе.
        logger.warning("RabbitMQ reconnected: %s", connection)

    @staticmethod
    def _on_close(
        connection: aio_pika.abc.AbstractConnection | None,
        exc: BaseException | None,
    ) -> None:
        if exc:
            logger.warning("RabbitMQ connection closed: %s", exc)
        else:
            logger.info("RabbitMQ connection closed gracefully")

    async def publish(self, queue_name, message):
        if not self.channel:
            raise Exception("RabbitMQ connection is not established.")

        # bug_239 audit 2026-05-28: workflow-очередь с DLX. Producer
        # должен объявить очередь с теми же аргументами, что и
        # consumer (иначе RabbitMQ кинет PRECONDITION_FAILED при
        # mismatch).
        queue = await declare_workflow_queue(self.channel, queue_name)
        msg = aio_pika.Message(
            body=message.encode(), delivery_mode=aio_pika.DeliveryMode.PERSISTENT
        )
        await self.channel.default_exchange.publish(
            msg,
            routing_key=queue.name,
        )

    async def declare_fanout_exchange(self, exchange_name: str):
        if not self.channel:
            raise Exception("RabbitMQ connection is not established.")

        exchange = await self.channel.declare_exchange(
            exchange_name, aio_pika.ExchangeType.FANOUT, durable=True
        )

        return exchange

    async def publish_to_exchange(
        self, exchange_name: str, message: str, routing_key: str = ""
    ):
        if not self.channel:
            raise Exception("RabbitMQ connection is not established.")

        exchange = await self.channel.declare_exchange(
            exchange_name, aio_pika.ExchangeType.FANOUT, durable=True
        )
        msg = aio_pika.Message(
            body=message.encode(), delivery_mode=aio_pika.DeliveryMode.PERSISTENT
        )

        await exchange.publish(msg, routing_key=routing_key)

    async def close(self):
        if self.connection:
            await self.connection.close()


rabbit_service = RabbitMQService()
