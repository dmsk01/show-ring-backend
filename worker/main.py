import asyncio

import aio_pika

from worker.handlers.book_handler import process_book

import json

async def process_message(message: aio_pika.IncomingMessage):
    async with message.process():
        body = message.body.decode()
        print(f"Received message: {body}")

        # Парсим JSON
        data = json.loads(body)
        task_id = data["task_id"]
        payload = data["payload"]

        # Вызываем обработчик
        await process_book(task_id, payload)

async def main():
    connection = await aio_pika.connect_robust("amqp://guest:guest@localhost/")
    channel = await connection.channel()
    
    await channel.set_qos(prefetch_count=1)
    queue = await channel.declare_queue("book_task", durable=True)
    await queue.consume(process_message)
    print("Waiting for book task messages. To exit press CTRL+C")

    try:
        await asyncio.Future()  # Run forever
    finally:
        await connection.close()

if __name__ == "__main__":
    asyncio.run(main())    