# Структура проекта ShowTail

```
showtail/
├── app/                            # FastAPI backend
│   ├── main.py                     # Точка входа, lifespan, middleware
│   ├── config.py                   # Настройки из .env (pydantic-settings)
│   ├── database.py                 # SQLAlchemy engine, async session
│   ├── redis.py                    # Redis client (async)
│   ├── dependencies.py             # Dependency Injection (get_db, get_current_user, ...)
│   ├── exceptions.py               # Кастомные исключения
│   │
│   ├── models/                     # SQLAlchemy ORM-модели
│   │   ├── base.py                 # DeclarativeBase, общие миксины (timestamps, soft delete)
│   │   ├── user.py                 # User, UserRole, RefreshToken, EmailVerificationToken
│   │   ├── kennel.py               # Kennel
│   │   ├── dog.py                  # Dog, DogTitle, DogPhoto
│   │   ├── litter.py               # Litter
│   │   ├── show.py                 # Show, ShowBreed, ShowRing, ShowJudge, ShowEntry, ShowResult
│   │   ├── classified.py           # Classified, ClassifiedImage
│   │   ├── payment.py              # Payment
│   │   ├── ad.py                   # AdCampaign, AdBanner, AdEvent
│   │   ├── notification.py         # Notification, Subscription
│   │   ├── support.py              # SupportTicket, SupportMessage
│   │   ├── task.py                 # Task (фоновые задачи)
│   │   ├── file.py                 # UploadedFile (реестр файлов)
│   │   └── reference.py            # AnimalType, Breed, BreedGroup, ShowClass,
│   │                               # ShowRank, Title, Grade
│   │
│   ├── schemas/                    # Pydantic-схемы (request/response)
│   │   ├── user.py
│   │   ├── kennel.py
│   │   ├── dog.py
│   │   ├── show.py
│   │   ├── classified.py
│   │   ├── payment.py
│   │   ├── ad.py
│   │   ├── notification.py
│   │   ├── support.py
│   │   ├── file.py
│   │   ├── reference.py
│   │   └── task.py                 # TaskStatus, TaskMessage (для RabbitMQ)
│   │
│   ├── routers/                    # HTTP-эндпоинты
│   │   ├── auth.py                 # POST /auth/register, /auth/login, /auth/refresh, /auth/verify-email
│   │   ├── users.py                # GET/PUT /users/me, GET /users/{id}
│   │   ├── kennels.py              # CRUD /kennels
│   │   ├── dogs.py                 # CRUD /dogs
│   │   ├── litters.py              # CRUD /litters
│   │   ├── shows.py                # CRUD /shows, /shows/{id}/entries, /shows/{id}/results
│   │   ├── classifieds.py          # CRUD /classifieds
│   │   ├── payments.py             # /payments (статусы, подтверждения)
│   │   ├── ads.py                  # CRUD /ads (рекламодатель), GET /ads/serve (показ)
│   │   ├── notifications.py        # /notifications, /subscriptions
│   │   ├── support.py              # /support/tickets, WebSocket /support/ws
│   │   ├── documents.py            # /shows/{id}/catalog/generate, /tasks/{id}
│   │   ├── files.py                # POST /files/upload, GET /files/{id}
│   │   ├── references.py           # GET /breeds, /show-classes, /ranks, ...
│   │   └── admin/                  # Админские эндпоинты
│   │       ├── references.py       # CRUD справочников
│   │       ├── moderation.py       # Модерация контента
│   │       └── analytics.py        # Дашборды и отчёты
│   │
│   ├── services/                   # Бизнес-логика
│   │   ├── auth.py                 # Регистрация, логин, JWT, email verification
│   │   ├── kennel.py               # Логика питомников
│   │   ├── dog.py                  # Логика собак и родословных
│   │   ├── show.py                 # Логика выставок и регистрации
│   │   ├── show_rules.py           # Правила РКФ: валидация классов, присвоение титулов
│   │   ├── document.py             # Формирование данных для PDF
│   │   ├── classified.py           # Логика объявлений
│   │   ├── payment.py              # Логика платежей
│   │   ├── ad.py                   # Логика рекламы и таргетинга
│   │   ├── notification.py         # Формирование уведомлений, определение получателей
│   │   ├── file_storage.py         # Загрузка/отдача файлов (MinIO S3)
│   │   ├── rabbit.py               # RabbitMQ publisher
│   │   ├── email.py                # Формирование писем
│   │   └── scheduler.py            # APScheduler: напоминания, cron-задачи
│   │
│   ├── repositories/               # Слой доступа к данным (SQL-запросы)
│   │   ├── base.py                 # BaseRepository (generic CRUD)
│   │   ├── user.py
│   │   ├── kennel.py
│   │   ├── dog.py
│   │   ├── show.py
│   │   ├── classified.py
│   │   ├── payment.py
│   │   ├── ad.py
│   │   ├── notification.py
│   │   ├── support.py
│   │   └── analytics.py            # Raw SQL аналитические запросы
│   │
│   ├── middleware/                  # HTTP middleware
│   │   ├── progressive_ban.py      # Progressive rate limiting (Redis + 429 + Retry-After)
│   │   ├── request_id.py           # X-Request-ID
│   │   ├── sanitization.py         # Очистка HTML/XSS из пользовательского контента
│   │   └── error_handler.py        # Глобальный обработчик ошибок
│   │
│   └── utils/                      # Утилиты
│       ├── security.py             # Хеширование паролей, JWT helpers, magic bytes
│       └── pdf.py                  # PDF-генерация (каталоги, дипломы)
│
├── worker/                         # Фоновые обработчики (RabbitMQ consumers)
│   ├── main.py                     # Точка входа воркера
│   ├── config.py                   # Настройки воркера
│   └── handlers/
│       ├── document_handler.py     # Генерация PDF
│       ├── email_handler.py        # Отправка email
│       ├── file_handler.py         # Обработка файлов (ресайз, watermark)
│       ├── ad_handler.py           # Рекламная аналитика (batch + fraud check)
│       └── import_handler.py       # Импорт данных
│
├── migrations/                     # Alembic миграции
│   ├── env.py
│   ├── script.py.mako
│   └── versions/                   # Файлы миграций
│
├── scripts/                        # Вспомогательные скрипты
│   ├── seed_references.py          # Заполнение справочников начальными данными
│   ├── download_packages.sh        # Скачивание пакетов для офлайн-установки
│   └── entrypoint.sh               # Docker entrypoint (миграции + запуск)
│
├── tests/                          # Тесты
│   ├── conftest.py                 # Общие фикстуры (test DB, client, mocks)
│   ├── test_auth.py
│   ├── test_kennels.py
│   ├── test_dogs.py
│   ├── test_shows.py
│   ├── test_classifieds.py
│   └── test_show_rules.py          # Тесты правил РКФ
│
├── docs/plans/                     # Документация проекта
│
├── alembic.ini                     # Конфигурация Alembic
├── docker-compose.yml              # Все сервисы: postgres, rabbitmq, redis, minio, mailpit
├── docker-compose.dev.yml          # Override для разработки
├── Dockerfile                      # Multi-stage build
├── .env                            # Переменные окружения
├── .env.example                    # Пример переменных
├── .dockerignore
└── requirements.txt                # Все зависимости с комментариями по этапам
```

## Принципы организации

### Слои приложения

```
Router (HTTP) → Service (логика) → Repository (SQL) → Model (ORM)
      ↓              ↓                   ↓
   Schema        Exception           Database
  (Pydantic)     handling           (PostgreSQL)
      ↓
  Middleware
  (progressive ban, sanitization, request_id, error_handler)
```

- **Router** — принимает HTTP, валидирует через Pydantic, вызывает Service
- **Service** — бизнес-логика, оркестрация (может вызвать несколько Repository)
- **Repository** — SQL-запросы (ORM, Core, Raw), возвращает модели
- **Model** — SQLAlchemy ORM, описание таблиц и связей
- **Middleware** — progressive ban, sanitization, request ID, error handling

### Почему Repository отдельно от Service

- Service содержит бизнес-правила ("при записи на выставку проверить возраст собаки")
- Repository содержит SQL ("SELECT dogs WHERE breed_id = ? AND birth_date > ?")
- Позволяет тестировать бизнес-логику отдельно от БД
- В Repository естественно группировать Raw SQL, Core и ORM запросы

### Инфраструктурные сервисы

| Сервис | Назначение | Docker-контейнер |
|--------|-----------|-----------------|
| PostgreSQL | Основное хранилище данных | `postgres:16` |
| RabbitMQ | Очереди задач, события | `rabbitmq:3-management` |
| Redis | Rate limiting, кэш, WS pub/sub | `redis:7-alpine` |
| MinIO | S3-совместимое файловое хранилище | `minio/minio` |
| MailPit | SMTP-заглушка для разработки | `axllent/mailpit` |
