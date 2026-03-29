# Этап 3: Pub/Sub (Fanout Exchange)

### Цель
Понять разницу между прямой очередью и exchange. Реализовать broadcast — отправку одного сообщения всем подписчикам.

### Почему Fanout важен

**Сценарий: Инвалидация кэша**

У вас 5 серверов API за балансировщиком. Каждый кэширует данные локально. Когда данные меняются, ВСЕ серверы должны очистить кэш.

```
[Admin API]
     │
     │ "Книга #42 обновлена"
     ▼
┌─────────────┐
│   Fanout    │
│  Exchange   │
└──────┬──────┘
       │
   ┌───┴───┬───────┬───────┬───────┐
   │       │       │       │       │
   ▼       ▼       ▼       ▼       ▼
[API-1] [API-2] [API-3] [API-4] [API-5]
   │       │       │       │       │
   └───────┴───────┴───────┴───────┘
         Все очистили кэш книги #42
```

**Без Fanout пришлось бы:**
- Знать IP всех серверов
- Отправлять HTTP запрос каждому
- Обрабатывать отказы, retry

**С Fanout:**
- Публикуем одно сообщение
- RabbitMQ доставляет всем подписчикам
- Подписчики могут добавляться/удаляться динамически

### Реальные кейсы использования

| Сценарий | Описание |
|----------|----------|
| **Инвалидация кэша** | Все инстансы очищают кэш при изменении данных |
| **Real-time уведомления** | Все WebSocket серверы рассылают событие клиентам |
| **Аудит/Логирование** | Каждый сервис логирует события независимо |
| **Мониторинг** | Метрики собираются несколькими системами |
| **Feature flags** | Все сервисы узнают об изменении флага мгновенно |

### Теория: Queue vs Exchange

**Direct Queue (то, что было в Этапах 1-2):**
```
[Producer] ──message──> [Queue] ──message──> [Consumer]

Одно сообщение = один получатель
Если 3 консьюмера — сообщение получит только один (round-robin)
```

**Fanout Exchange:**
```
                         ┌──> [Queue A] ──> [Consumer A]
[Producer] ──> [Exchange]├──> [Queue B] ──> [Consumer B]
                         └──> [Queue C] ──> [Consumer C]

Одно сообщение = копия КАЖДОМУ подписчику
Каждый консьюмер имеет свою очередь
```

### Временные очереди

Для Fanout часто используют **временные очереди**:

```python
queue = await channel.declare_queue(
    "",              # Пустое имя — RabbitMQ сгенерирует уникальное
    exclusive=True,  # Только это соединение может читать
    auto_delete=True # Удалить когда консьюмер отключится
)
```

**Почему временные:**
- Fanout-подписчик обычно временный (инстанс сервера)
- При рестарте нужна новая очередь с актуальными сообщениями
- Не засоряем RabbitMQ мёртвыми очередями

### Файлы для создания/изменения

#### `app/services/rabbit.py` — добавить методы

```python
# Добавить в класс RabbitMQService:

async def declare_fanout_exchange(self, exchange_name: str) -> aio_pika.Exchange:
    """
    Создать exchange типа fanout.

    durable=True означает что exchange переживёт рестарт RabbitMQ.
    Это важно для production — не хотим терять конфигурацию.
    """
    if not self.channel:
        raise RabbitMQConnectionError("Not connected")

    exchange = await self.channel.declare_exchange(
        exchange_name,
        aio_pika.ExchangeType.FANOUT,
        durable=True
    )
    return exchange

async def publish_to_exchange(
    self,
    exchange_name: str,
    message: str,
    routing_key: str = ""
) -> None:
    """
    Отправить сообщение в exchange.

    Для fanout routing_key игнорируется, но передаём пустую строку.
    Для topic routing_key будет использоваться для маршрутизации.
    """
    if not self.channel:
        raise RabbitMQConnectionError("Not connected")

    # Получаем exchange (он должен быть уже создан)
    # Или создаём если не существует
    exchange = await self.channel.declare_exchange(
        exchange_name,
        aio_pika.ExchangeType.FANOUT,
        durable=True
    )

    msg = aio_pika.Message(
        body=message.encode(),
        delivery_mode=aio_pika.DeliveryMode.PERSISTENT
    )

    await exchange.publish(msg, routing_key=routing_key)
```

#### `app/schemas/event.py`

```python
"""
Модели для событий (Pub/Sub).

События отличаются от задач:
- Задача: "сделай что-то" (команда)
- Событие: "произошло что-то" (факт)

Задачу обрабатывает ОДИН воркер.
Событие получают ВСЕ подписчики.
"""
from pydantic import BaseModel, Field
from datetime import datetime
from typing import Any

class Event(BaseModel):
    """
    Базовая модель события.

    Паттерн: Event Sourcing light
    Каждое событие содержит:
    - Тип (что произошло)
    - Данные (детали)
    - Метаданные (когда, кто, откуда)
    """
    event_type: str = Field(
        ...,
        description="Тип события: book.created, cache.invalidate, user.logged_in"
    )
    data: dict[str, Any] = Field(
        default_factory=dict,
        description="Полезные данные события"
    )
    timestamp: datetime = Field(
        default_factory=datetime.utcnow,
        description="Время создания события"
    )
    source: str = Field(
        default="api",
        description="Источник события (сервис)"
    )

class CacheInvalidateEvent(Event):
    """Событие инвалидации кэша"""
    event_type: str = "cache.invalidate"

class BookCreatedEvent(Event):
    """Событие создания книги"""
    event_type: str = "book.created"
```

#### `app/routers/events.py`

```python
"""
Роутер для демонстрации Pub/Sub.

Реальные сценарии:
- POST /events/cache/invalidate — очистить кэш на всех инстансах
- POST /events/broadcast — отправить произвольное событие
"""
from fastapi import APIRouter, Depends
from typing import Annotated
import logging

from app.schemas.event import Event
from app.services.rabbit import RabbitMQService, rabbit_service
from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/events", tags=["events"])

RabbitDep = Annotated[RabbitMQService, Depends(lambda: rabbit_service)]

@router.post("/broadcast")
async def broadcast_event(event: Event, rabbit: RabbitDep) -> dict:
    """
    Отправить событие всем подписчикам (fanout).

    Все воркеры/сервисы, подписанные на exchange "events",
    получат копию этого сообщения.
    """
    await rabbit.publish_to_exchange(
        settings.exchange_events,
        event.model_dump_json()
    )

    logger.info(f"Event broadcasted: {event.event_type}")

    return {
        "status": "broadcasted",
        "event_type": event.event_type,
        "receivers": "all subscribers"
    }

@router.post("/cache/invalidate")
async def invalidate_cache(
    entity_type: str,
    entity_id: int,
    rabbit: RabbitDep
) -> dict:
    """
    Инвалидировать кэш на всех инстансах.

    Практический пример fanout:
    - Admin обновил книгу #42
    - Все API серверы должны очистить кэш этой книги
    """
    event = Event(
        event_type="cache.invalidate",
        data={"entity_type": entity_type, "entity_id": entity_id},
        source="admin_api"
    )

    await rabbit.publish_to_exchange(
        settings.exchange_events,
        event.model_dump_json()
    )

    return {
        "status": "cache invalidation sent",
        "entity": f"{entity_type}:{entity_id}"
    }
```

#### `worker/main.py` — добавить режим fanout

```python
# Добавить функцию и модифицировать main()

import argparse

async def subscribe_to_events() -> None:
    """
    Подписаться на все события через fanout exchange.

    Каждый воркер создаёт свою временную очередь.
    При отключении очередь удаляется.
    """
    logger.info("Starting in fanout subscriber mode...")

    connection = await aio_pika.connect_robust(RABBITMQ_URL)
    channel = await connection.channel()

    # 1. Объявить exchange (должен совпадать с API)
    exchange = await channel.declare_exchange(
        "events",
        aio_pika.ExchangeType.FANOUT,
        durable=True
    )

    # 2. Создать временную очередь
    queue = await channel.declare_queue(
        "",              # Пустое имя — сгенерируется автоматически
        exclusive=True,  # Только этот консьюмер
        auto_delete=True # Удалить при отключении
    )

    # 3. Привязать очередь к exchange
    await queue.bind(exchange)

    logger.info(f"Subscribed to events. Queue: {queue.name}")

    async def on_event(message: IncomingMessage) -> None:
        async with message.process():
            event_data = json.loads(message.body.decode())
            event_type = event_data.get("event_type", "unknown")

            logger.info(
                f"Received event: {event_type}",
                extra={"event": event_data}
            )

            # Здесь обработка события
            # Например, очистка кэша:
            if event_type == "cache.invalidate":
                entity = event_data.get("data", {})
                logger.info(f"Invalidating cache for {entity}")
                # cache.delete(entity_type, entity_id)

    await queue.consume(on_event)

    # Ждём сигнала остановки
    stop_event = asyncio.Event()
    try:
        await stop_event.wait()
    except KeyboardInterrupt:
        pass
    finally:
        await connection.close()

# Модифицировать main():
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RabbitMQ Worker")
    parser.add_argument(
        "--mode",
        choices=["queue", "fanout", "topic"],
        default="queue",
        help="Worker mode: queue (tasks), fanout (events), topic (routing)"
    )
    parser.add_argument(
        "--pattern",
        default="#",
        help="Topic pattern for topic mode (e.g., 'book.*', '*.created')"
    )
    args = parser.parse_args()

    if args.mode == "fanout":
        asyncio.run(subscribe_to_events())
    elif args.mode == "topic":
        asyncio.run(subscribe_to_topic(args.pattern))
    else:
        asyncio.run(main())
```

### Как проверить Этап 3

1. Запустить 3 терминала с воркерами в режиме fanout:
   ```bash
   python -m worker.main --mode fanout  # Терминал 1
   python -m worker.main --mode fanout  # Терминал 2
   python -m worker.main --mode fanout  # Терминал 3
   ```

2. В RabbitMQ Management UI (http://localhost:15672):
   - Exchanges → увидишь "events" типа fanout
   - Queues → 3 временных очереди с автоименами (amq.gen-...)

3. POST /events/broadcast:
   ```json
   {
     "event_type": "book.created",
     "data": {"book_id": 42, "title": "New Book"}
   }
   ```

4. Все 3 воркера напечатают одно и то же сообщение

5. Остановить один воркер (Ctrl+C) — его очередь исчезнет из UI
