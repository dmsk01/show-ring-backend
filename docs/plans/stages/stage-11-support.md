# Этап 11: Онлайн-поддержка

### Цель

Реализовать систему онлайн-поддержки: тикеты, real-time чат через WebSocket, уведомления операторам.

### Что появляется в проекте

- Тикет-система: пользователь создаёт обращение, оператор отвечает
- Real-time чат через WebSocket (внутри тикета)
- Статусы тикетов: open → in_progress → resolved → closed
- Назначение оператора на тикет
- Уведомление оператору о новом тикете через RabbitMQ
- История обращений пользователя

### Модель данных

Новые таблицы: `support_tickets`, `support_messages`

support_tickets:
- user_id, subject, status, priority, assigned_to_id

support_messages:
- ticket_id, sender_id, message, is_from_operator

### API эндпоинты

| Метод | Путь | Описание | Доступ |
|-------|------|----------|--------|
| POST | `/support/tickets` | Создать тикет | Authenticated |
| GET | `/support/tickets` | Мои тикеты | Authenticated |
| GET | `/support/tickets/{id}` | Тикет + история сообщений | Owner / Operator |
| PUT | `/support/tickets/{id}/status` | Сменить статус | Operator |
| PUT | `/support/tickets/{id}/assign` | Назначить оператора | Admin |
| POST | `/support/tickets/{id}/messages` | Отправить сообщение (REST fallback) | Owner / Operator |
| WS | `/support/ws/{ticket_id}` | Real-time чат | Owner / Operator |
| GET | `/admin/support/tickets` | Все тикеты (фильтры) | Admin / Operator |

### WebSocket чат

```
Client                    Server
  │                          │
  │── WS connect ──────────>│  (без авторизации — просто handshake)
  │<── connection_ack ──────│
  │                          │
  │── {"type": "auth",      │  (JWT передаётся ПЕРВЫМ СООБЩЕНИЕМ,
  │    "token": "eyJ..."}──>│   НЕ в URL / query params — токен в URL
  │                          │   попадает в логи и browser history)
  │<── {"type": "auth_ok"}──│
  │                          │
  │── {"text": "Помогите"}──>│  (сохранить в БД, publish в Redis)
  │                          │
  │<── {"text": "Что случилось?", │  (получено из Redis pub/sub)
  │     "from": "operator"}──│
  │                          │
  │── close ────────────────>│
```

> **Почему Redis Pub/Sub:** при масштабировании (`--scale api=3`) WebSocket-соединения распределены по разным инстансам. Без Redis сообщение оператора не дойдёт до пользователя на другом инстансе. Redis Pub/Sub решает это — все инстансы подписаны на канал тикета.

### Файлы для создания

| Файл | Назначение |
|------|-----------|
| `app/models/support.py` | SupportTicket, SupportMessage ORM |
| `app/schemas/support.py` | Pydantic-схемы |
| `app/routers/support.py` | REST + WebSocket эндпоинты |
| `app/services/support.py` | Бизнес-логика тикетов |
| `app/repositories/support.py` | SQL-запросы |

### Ключевые концепции

- **WebSocket в FastAPI** — `@app.websocket("/support/ws/{ticket_id}")`
- **JWT через первое сообщение** — НЕ в query params (безопасность: URL логируется)
- **Redis Pub/Sub** — доставка сообщений между инстансами API при масштабировании
- **Connection Manager** — хранение активных WebSocket соединений (per-instance)
- **Уведомления через RabbitMQ** — при создании тикета воркер уведомляет оператора
- **Graceful disconnect** — обработка разрыва соединения

### SQL-фокус

| Что изучаем | Как |
|-------------|-----|
| Пагинация сообщений | Загрузка истории: ORDER BY created_at DESC LIMIT 50 |
| Фильтрация тикетов (Core) | По статусу + приоритету + оператору |
| Подсчёт непрочитанных | COUNT messages WHERE is_read = false |

### Как проверить

1. `POST /support/tickets` — создать тикет
2. Подключиться через WebSocket клиент к `/support/ws/{ticket_id}`
3. Отправить сообщение → оно сохраняется в БД
4. Оператор подключается к тому же тикету → видит сообщение
5. Оператор отвечает → пользователь получает в real-time
6. `GET /support/tickets/{id}` — вся история сообщений (REST fallback)
