import asyncio
from aio_pika import connect_robust, IncomingMessage


async def process_message(message: IncomingMessage):
    async with message.process():
        body = message.body.decode()
        print(f"Received message: {body}")


async def main():
    connection = await connect_robust("amqp://guest:guest@localhost/")
    channel = await connection.channel()

    await channel.set_qos(prefetch_count=1)
    queue = await channel.declare_queue("tasks", durable=True)
    await queue.consume(process_message)
    print("Waiting for messages. To exit press CTRL+C")

    try:
        await asyncio.Future()  # Run forever
    finally:
        await connection.close()


if __name__ == "__main__":
    asyncio.run(main())
