"""
Учебный standalone-воркер. Production-точка входа — worker/main.py.
Оставлен для песочницы; print заменён на logger чтобы не «портить»
JSON-логи в проде, если кто-то случайно запустит этот файл.
"""
import asyncio
import logging

from aio_pika import connect_robust, IncomingMessage

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


async def process_message(message: IncomingMessage):
    async with message.process():
        body = message.body.decode()
        logger.info("Received message: %s", body)


async def main():
    connection = await connect_robust("amqp://guest:guest@localhost/")
    channel = await connection.channel()

    await channel.set_qos(prefetch_count=1)
    queue = await channel.declare_queue("tasks", durable=True)
    await queue.consume(process_message)
    logger.info("Waiting for messages. To exit press CTRL+C")

    try:
        await asyncio.Future()  # Run forever
    finally:
        await connection.close()


if __name__ == "__main__":
    asyncio.run(main())
