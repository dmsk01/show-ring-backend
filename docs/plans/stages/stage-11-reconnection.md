# Этап 11: Reconnection и Connection Recovery

### Цель

Научить `RabbitMQService` автоматически восстанавливать соединение при разрыве. В production RabbitMQ может перезагружаться, сеть может моргать.

### Проблема

Текущий `RabbitMQService` подключается один раз в `lifespan`. Если соединение упало — все последующие `publish()` падают с ошибкой, и приложение нужно перезапускать вручную.

### Решение: Robust Connection через aio-pika

aio-pika имеет встроенный механизм — `connect_robust()`:

```python
# app/services/rabbit.py
import logging
import aio_pika
from aio_pika import RobustConnection, RobustChannel

logger = logging.getLogger(__name__)

class RabbitMQService:
    """
    RabbitMQ сервис с автоматическим переподключением.

    connect_robust() от aio-pika:
    - Автоматически переподключается при разрыве
    - Переоткрывает каналы
    - Переобъявляет очереди и exchange
    """

    def __init__(self):
        self.connection: RobustConnection | None = None
        self.channel: RobustChannel | None = None

    async def connect(self, url: str) -> None:
        self.connection = await aio_pika.connect_robust(
            url,
            reconnect_interval=5,  # Интервал между попытками (сек)
            fail_fast=True,        # Упасть при первой ошибке подключения
        )
        self.channel = await self.connection.channel()
        self.connection.reconnect_callbacks.add(self._on_reconnect)
        logger.info("Connected to RabbitMQ (robust mode)")

    async def _on_reconnect(self, connection) -> None:
        """Вызывается при каждом переподключении."""
        logger.warning("RabbitMQ reconnected")

    async def publish(self, queue_name: str, message: str) -> None:
        if not self.channel:
            raise RuntimeError("RabbitMQ not connected")

        queue = await self.channel.declare_queue(queue_name, durable=True)

        await self.channel.default_exchange.publish(
            aio_pika.Message(
                body=message.encode(),
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            ),
            routing_key=queue.name,
        )
        logger.info("Message published", extra={"queue": queue_name})

    async def close(self) -> None:
        if self.connection and not self.connection.is_closed:
            await self.connection.close()
            logger.info("RabbitMQ connection closed")
```

### Как тестировать reconnection

1. Запустить API и воркер
2. Создать книгу — убедиться что работает
3. Перезапустить RabbitMQ: `rabbitmqctl stop_app && rabbitmqctl start_app`
4. Подождать 5-10 секунд (reconnect_interval)
5. Создать ещё одну книгу — должно работать без перезапуска приложения

### Reconnection в воркере

```python
# worker/main.py
async def main():
    connection = await aio_pika.connect_robust(
        settings.rabbitmq_url,
        reconnect_interval=5,
    )
    channel = await connection.channel()
    await channel.set_qos(prefetch_count=1)

    queue = await channel.declare_queue("book_tasks", durable=True)

    logger.info("Worker started, waiting for messages...")

    async with queue.iterator() as queue_iter:
        async for message in queue_iter:
            async with message.process():
                await handle_message(message)
```

### Важные нюансы

| Параметр | Описание | Рекомендация |
|----------|---------|-------------|
| `reconnect_interval` | Пауза между попытками | 5 секунд |
| `fail_fast` | Падать если первое подключение не удалось | `True` для startup, `False` для worker |
| `connection_timeout` | Таймаут на подключение | 10-30 секунд |
| `heartbeat` | Проверка живости соединения | 60 секунд (по умолчанию) |

### Что делать

1. Заменить `aio_pika.connect()` на `aio_pika.connect_robust()` в `app/services/rabbit.py`
2. Добавить `reconnect_callbacks` для логирования
3. Заменить `aio_pika.connect()` на `connect_robust()` в `worker/main.py`
4. Протестировать: создать книгу -> перезапустить RabbitMQ -> создать ещё книгу
