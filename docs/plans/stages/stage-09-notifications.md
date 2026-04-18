# Этап 9: Уведомления и события (RabbitMQ)

### Цель

Реализовать систему уведомлений через RabbitMQ: email-рассылки через Task Queue, broadcast событий через Fanout/Topic Exchange. Добавить планировщик задач (APScheduler) для отложенных уведомлений. Подключить MailPit как SMTP-заглушку для разработки.

### Что появляется в проекте

- **MailPit** в docker-compose — SMTP-заглушка для разработки (UI на порту 8025)
- **APScheduler** — планировщик для отложенных задач:
  - Напоминание о выставке (за 7 дней и за 1 день)
  - Закрытие просроченных объявлений
  - Очистка expired refresh tokens
- Email-уведомления:
  - Подтверждение записи на выставку
  - Напоминание о выставке (за 7 дней, за 1 день)
  - Результаты выставки участникам
  - Новый помёт интересующей породы (подписка)
- Fanout Exchange — broadcast:
  - Выставка: регистрация открыта → все подписчики
  - Результаты опубликованы → все участники + подписчики
- Topic Exchange — маршрутизация по интересам:
  - `show.registration_opened` → подписчики на выставки
  - `litter.announced.breed.{breed_id}` → подписчики на породу
  - `dog.title_earned` → профиль собаки, профиль питомника
- Таблица подписок пользователей (на породу, на регион, на питомник)

### Модель данных

Новые таблицы:

**`notifications`** — лог отправленных уведомлений
| Колонка | Тип | Описание |
|---------|-----|----------|
| id | UUID (PK) | |
| user_id | FK → users | |
| type | VARCHAR(50) | email, push |
| subject | VARCHAR(300) | |
| status | ENUM | pending, sent, failed |
| sent_at | TIMESTAMPTZ | |
| created_at | TIMESTAMPTZ | |

**`subscriptions`** — подписки пользователей
| Колонка | Тип | Описание |
|---------|-----|----------|
| id | UUID (PK) | |
| user_id | FK → users | |
| event_type | VARCHAR(50) | show_opened, litter_announced, ... |
| filter_breed_id | FK → breeds | NULL = все породы |
| filter_region | VARCHAR(100) | NULL = все регионы |
| channel | ENUM | email, push |
| is_active | BOOLEAN | |

### API эндпоинты

| Метод | Путь | Описание | Доступ |
|-------|------|----------|--------|
| GET | `/notifications` | Мои уведомления | Authenticated |
| POST | `/subscriptions` | Подписаться на события | Authenticated |
| GET | `/subscriptions` | Мои подписки | Authenticated |
| DELETE | `/subscriptions/{id}` | Отписаться | Authenticated |

### Очереди и exchanges

```
Task Queue:
  email_tasks → worker/handlers/email_handler.py

Fanout Exchange:
  "broadcast" → все подключённые сервисы получают копию

Topic Exchange:
  "events" с routing keys:
    show.registration_opened
    show.results_published
    litter.announced.breed.{breed_id}
    dog.title_earned
```

### Файлы для создания

| Файл | Назначение |
|------|-----------|
| `app/models/notification.py` | Notification, Subscription ORM |
| `app/schemas/notification.py` | Pydantic-схемы |
| `app/routers/notifications.py` | Уведомления и подписки |
| `app/services/notification.py` | Логика: определить получателей, сформировать сообщение, опубликовать |
| `app/services/email.py` | Формирование email (subject, body, Jinja2 templates) |
| `app/services/scheduler.py` | APScheduler: настройка cron-задач |
| `worker/handlers/email_handler.py` | SMTP-отправка (aiosmtplib → MailPit в dev, реальный SMTP в prod) |

### Интеграция с существующими сервисами

Добавить публикацию событий в:
- `app/services/show.py` — при открытии регистрации, публикации результатов
- `app/services/dog.py` — при получении титула
- `app/services/classified.py` — при публикации помёта

### Ключевые концепции

- **Fanout Exchange** — одно сообщение → копия каждому подписчику
- **Topic Exchange** — routing key + pattern matching (`show.*`, `litter.announced.breed.*`)
- **Временные очереди** — `exclusive=True, auto_delete=True` для fanout
- **Именованные очереди с паттернами** — для topic (persistence между рестартами)
- **Throttling** — ограничение скорости отправки email (не более N/мин)
- **APScheduler** — AsyncIOScheduler для cron-like задач (напоминания, очистка)
- **MailPit** — SMTP-заглушка для dev (http://localhost:8025 — UI для просмотра писем)
- **aiosmtplib** — async SMTP клиент для отправки email
- **Jinja2** — шаблоны писем (HTML)

### SQL-фокус

| Что изучаем | Как |
|-------------|-----|
| Подзапросы для подписчиков | `SELECT users WHERE id IN (SELECT user_id FROM subscriptions WHERE event_type = ... AND breed_id = ...)` |
| Batch INSERT | Массовая вставка уведомлений для всех подписчиков |
| INDEX на составной ключ | subscriptions(event_type, filter_breed_id) |

### Как проверить

1. `POST /subscriptions` — подписаться на `litter.announced` для породы "Хаски"
2. `POST /litters` — заводчик публикует помёт хаски
3. Проверить: в очереди email_tasks появилось сообщение
4. Воркер отправляет email (или логирует в dev-режиме)
5. `GET /notifications` — уведомление появилось в списке
6. Запустить 3 воркера в режиме fanout → все получают broadcast
7. Запустить воркеры с разными паттернами topic → каждый получает только своё
