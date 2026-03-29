# Этап 4: Routing (Topic Exchange)

### Цель
Реализовать гибкую маршрутизацию сообщений по паттернам. Разные воркеры получают разные типы событий.

### Почему Topic Exchange важен

**Сценарий: Микросервисная архитектура**

У вас несколько сервисов, каждый заинтересован в своих событиях:

```
События:
- book.created     ─┐
- book.updated     ─┼─ Book Service (pattern: book.*)
- book.deleted     ─┘

- user.created     ─┐
- user.logged_in   ─┼─ User Service (pattern: user.*)
- user.deleted     ─┘

- *.created        ───── Audit Service (логирует все создания)
- #                ───── Monitoring (получает ВСЁ)
```

**Преимущества:**
- Сервисы независимы друг от друга
- Добавление нового подписчика не требует изменения publisher
- Фильтрация на уровне брокера (эффективно)

### Routing Key и паттерны

**Routing Key** — строка с точками-разделителями:
```
entity.action
book.created
user.profile.updated
order.payment.failed
```

**Паттерны:**
- `*` — ровно одно слово
- `#` — ноль или более слов

**Примеры матчинга:**

| Routing Key | `book.*` | `*.created` | `book.#` | `#` |
|-------------|----------|-------------|----------|-----|
| `book.created` | ✅ | ✅ | ✅ | ✅ |
| `book.deleted` | ✅ | ❌ | ✅ | ✅ |
| `book.chapter.added` | ❌ | ❌ | ✅ | ✅ |
| `user.created` | ❌ | ✅ | ❌ | ✅ |
| `order.payment.failed` | ❌ | ❌ | ❌ | ✅ |

### Файлы для создания/изменения

#### `app/services/rabbit.py` — добавить методы для topic

```python
async def declare_topic_exchange(self, exchange_name: str) -> aio_pika.Exchange:
    """
    Создать exchange типа topic.

    Topic exchange использует routing key для маршрутизации.
    Подписчики указывают паттерн (book.*, *.created, #).
    """
    if not self.channel:
        raise RabbitMQConnectionError("Not connected")

    exchange = await self.channel.declare_exchange(
        exchange_name,
        aio_pika.ExchangeType.TOPIC,
        durable=True
    )
    return exchange

async def publish_with_routing_key(
    self,
    exchange_name: str,
    routing_key: str,
    message: str
) -> None:
    """
    Отправить сообщение с routing key.

    routing_key определяет в какие очереди попадёт сообщение.
    Exchange сравнивает routing_key с паттернами привязок.
    """
    if not self.channel:
        raise RabbitMQConnectionError("Not connected")

    exchange = await self.channel.declare_exchange(
        exchange_name,
        aio_pika.ExchangeType.TOPIC,
        durable=True
    )

    msg = aio_pika.Message(
        body=message.encode(),
        delivery_mode=aio_pika.DeliveryMode.PERSISTENT
    )

    await exchange.publish(msg, routing_key=routing_key)

    logger.info(f"Published to {exchange_name} with routing_key={routing_key}")
```

#### `app/routers/books.py` — использовать topic exchange

```python
# Модифицировать create_book и добавить delete_book с events:

EXCHANGE_NAME = "app_events"

@router.post("", response_model=BookWithTask, status_code=201)
async def create_book(
    book: BookCreate,
    rabbit: RabbitDep,
    storage: StorageDep
) -> BookWithTask:
    # ... создание книги ...

    # Отправить событие через topic exchange
    event = Event(
        event_type="book.created",
        data={"book_id": new_id, "title": new_book["title"]}
    )
    await rabbit.publish_with_routing_key(
        EXCHANGE_NAME,
        "book.created",  # routing key
        event.model_dump_json()
    )

    return BookWithTask(...)

@router.delete("/{book_id}", status_code=204)
async def delete_book(book_id: int, rabbit: RabbitDep) -> None:
    # ... удаление книги ...

    event = Event(
        event_type="book.deleted",
        data={"book_id": book_id}
    )
    await rabbit.publish_with_routing_key(
        EXCHANGE_NAME,
        "book.deleted",
        event.model_dump_json()
    )
```

#### `worker/main.py` — режим topic с паттерном

```python
async def subscribe_to_topic(pattern: str) -> None:
    """
    Подписаться на события по паттерну.

    pattern: "book.*", "*.created", "#"
    """
    logger.info(f"Starting in topic subscriber mode with pattern: {pattern}")

    connection = await aio_pika.connect_robust(RABBITMQ_URL)
    channel = await connection.channel()

    # 1. Объявить topic exchange
    exchange = await channel.declare_exchange(
        "app_events",
        aio_pika.ExchangeType.TOPIC,
        durable=True
    )

    # 2. Создать именованную очередь (для persistence)
    # Имя очереди генерируем из паттерна
    safe_pattern = pattern.replace("*", "star").replace("#", "hash").replace(".", "_")
    queue_name = f"worker_{safe_pattern}"

    queue = await channel.declare_queue(queue_name, durable=True)

    # 3. Привязать к exchange с паттерном
    await queue.bind(exchange, routing_key=pattern)

    logger.info(f"Bound to pattern '{pattern}' via queue '{queue_name}'")

    async def on_message(message: IncomingMessage) -> None:
        async with message.process():
            routing_key = message.routing_key
            body = message.body.decode()

            logger.info(
                f"Received [{routing_key}]: {body[:100]}",
                extra={"routing_key": routing_key}
            )

            # Обработка в зависимости от routing key
            if routing_key == "book.created":
                logger.info("Handling book creation...")
            elif routing_key == "book.deleted":
                logger.info("Handling book deletion...")
            # и т.д.

    await queue.consume(on_message)

    stop_event = asyncio.Event()
    try:
        await stop_event.wait()
    except KeyboardInterrupt:
        pass
    finally:
        await connection.close()
```

### Как проверить Этап 4

1. Запустить воркеров с разными паттернами:
   ```bash
   python -m worker.main --mode topic --pattern "book.*"     # Только книги
   python -m worker.main --mode topic --pattern "*.created"  # Только создания
   python -m worker.main --mode topic --pattern "#"          # Всё
   ```

2. POST /books (создание):
   - `book.*` — получит ✅
   - `*.created` — получит ✅
   - `#` — получит ✅

3. DELETE /books/1 (удаление):
   - `book.*` — получит ✅
   - `*.created` — НЕ получит ❌
   - `#` — получит ✅
