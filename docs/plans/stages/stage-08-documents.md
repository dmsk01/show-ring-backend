# Этап 8: Генерация документов (RabbitMQ)

### Цель

Подключить RabbitMQ для фоновой генерации PDF-документов: каталог выставки, дипломы участников, сертификаты титулов. Реализовать паттерн Task Queue со статус-машиной.

### Что появляется в проекте

- Подключение RabbitMQ (aio-pika, connect_robust)
- Task Queue: API публикует задачу → воркер генерирует PDF → результат сохраняется
- Статус-машина задач: pending → processing → done / failed
- Генерация PDF:
  - **Каталог выставки** — список участников по породам/классам, номера, владельцы
  - **Дипломы** — для каждого участника с оценкой и титулами
  - **Сертификаты** — CAC, CACIB, BOB и другие титулы
  - **Экспортная карточка** — для отправки документов в РКФ
- Скачивание готовых документов через API
- Lifespan: подключение/отключение RabbitMQ

### Модель данных

Новая таблица: `tasks` (статус фоновых задач)

| Колонка | Тип | Описание |
|---------|-----|----------|
| id | UUID (PK) | |
| type | VARCHAR(50) | generate_catalog, generate_diploma, ... |
| status | ENUM | pending, processing, done, failed |
| payload | JSONB | Входные данные (show_id, entry_id, ...) |
| result | JSONB | Результат (file_id, error_message, ...) |
| created_at | TIMESTAMPTZ | |
| updated_at | TIMESTAMPTZ | |

### API эндпоинты

| Метод | Путь | Описание | Доступ |
|-------|------|----------|--------|
| POST | `/shows/{id}/catalog/generate` | Запустить генерацию каталога | Organizer |
| POST | `/shows/{id}/diplomas/generate` | Генерация дипломов для всех | Organizer |
| POST | `/shows/{id}/entries/{eid}/diploma` | Диплом для одного участника | Organizer |
| GET | `/tasks/{task_id}` | Статус задачи | Authenticated |
| GET | `/tasks/{task_id}/download` | Скачать результат (PDF) | Authenticated |

### Файлы для создания

| Файл | Назначение |
|------|-----------|
| `app/services/rabbit.py` | RabbitMQService: connect, publish, close |
| `app/services/document.py` | Формирование данных для PDF (запросы из БД) |
| `app/schemas/task.py` | TaskMessage, TaskStatusResponse |
| `app/models/task.py` | Task ORM (статус в БД, не in-memory) |
| `app/routers/documents.py` | Эндпоинты генерации и скачивания |
| `app/repositories/task.py` | SQL: создание, обновление статуса задачи |
| `app/utils/pdf.py` | PDF-генерация (ReportLab / WeasyPrint) |
| `worker/main.py` | Точка входа воркера |
| `worker/handlers/document_handler.py` | Генерация каталогов, дипломов, сертификатов |
| `worker/handlers/file_handler.py` | Ресайз фото, watermark |

### Структура PDF-документов

**Каталог выставки:**
```
Заголовок: название, ранг, дата, место
Судьи: список с назначениями

По каждой породе (в порядке групп FCI):
  Порода: название (рус/англ), номер FCI
  Судья: ФИО
  По классам (бэби → ветераны):
    Класс: название
    Участники:
      №001 | Кличка | Дата рожд. | Окрас | Владелец | Заводчик
```

**Диплом:**
```
Название выставки, ранг, дата
Порода, класс
Кличка собаки, номер родословной
Оценка, место, титулы
ФИО судьи
```

### Ключевые концепции

- **Task Queue** — producer (API) / consumer (worker) через RabbitMQ
- **connect_robust** — автоматическое переподключение
- **Статус в PostgreSQL** — не in-memory, переживает рестарт
- **PDF generation** — ReportLab (программный) или WeasyPrint (из HTML/CSS)
- **Prefetch count** — контроль нагрузки на воркер

### SQL-фокус

| Что изучаем | Как |
|-------------|-----|
| Сложный SELECT для каталога (Raw SQL) | JOIN entries + dogs + breeds + breed_groups + classes + owners, ORDER BY group, breed, class, catalog_number |
| JSONB операции | payload/result в таблице tasks |
| UPDATE с условием | Обновление статуса задачи (оптимистическая блокировка) |

### Как проверить

1. Запустить воркер: `python -m worker.main`
2. `POST /shows/{id}/catalog/generate` — получить task_id
3. `GET /tasks/{task_id}` — pending → processing → done
4. `GET /tasks/{task_id}/download` — скачать PDF каталога
5. `POST /shows/{id}/diplomas/generate` — генерация дипломов
6. Перезапустить воркер во время генерации — задача не потеряется (persistent messages)
