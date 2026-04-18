import aio_pika


class RabbitMQService:
    def __init__(self):
        self.connection: aio_pika.Connection | None = None
        self.channel: aio_pika.Channel | None = None

    async def connect(self, url: str):
        self.connection = await aio_pika.connect_robust(url)
        self.channel = await self.connection.channel()

    async def publish(self, queue_name, message):
        if not self.channel:
            raise Exception("RabbitMQ connection is not established.")

        queue = await self.channel.declare_queue(queue_name, durable=True)
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
