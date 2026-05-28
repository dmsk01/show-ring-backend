from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")
    rabbitmq_url: str = "amqp://guest:guest@localhost/"
    exchange_events: str = "events"
    database_url: str
    secret_key: str
    redis_url: str = "redis://localhost:6379/0"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7
    # ИСПРАВЛЕНО: добавлен явный флаг debug — заменяет хардкод echo=True
    # и используется для отключения dev-эндпоинтов в проде.
    debug: bool = False
    # ИСПРАВЛЕНО: CORS вынесен в конфиг. Пустой список = CORS не включается,
    # чтобы не открывать API случайно. Список доменов задаётся через .env.
    cors_allow_origins: list[str] = []
    # ИСПРАВЛЕНО: внутренний API key для воркеров (раньше "secret-key" хардкод).
    internal_api_key: str | None = None

    # --- Этап 4: MinIO (S3-совместимое хранилище) ---
    # endpoint без https — в проде поменяется на реальный домен с TLS.
    # bucket пер-сервис: разные сервисы платформы не пересекаются по namespace.
    s3_endpoint: str = "http://127.0.0.1:9000"
    s3_access_key: str = "showtail"
    s3_secret_key: str = "showtailminio"
    s3_bucket: str = "showtail-files"
    s3_region: str = "us-east-1"
    # Максимальный размер загружаемого файла. Лимит чтобы не словить
    # OOM/диск-флуд: на CI/проде обычно ставится на reverse-proxy, но
    # дублируем на уровне приложения для defence-in-depth.
    max_upload_size_bytes: int = 10 * 1024 * 1024  # 10 МБ

    # --- Этап 9: Email + Scheduler ---
    # SMTP-настройки. В dev — MailPit (порт 1025, без auth, без TLS).
    # В prod — реальный SMTP (Sendgrid/Mailgun/SES) с STARTTLS и логином.
    # Дефолты подобраны под MailPit, чтобы локально всё работало "из коробки".
    smtp_host: str = "127.0.0.1"
    smtp_port: int = 1025
    smtp_username: str | None = None
    smtp_password: str | None = None
    # STARTTLS актуален для prod-SMTP. MailPit его не поддерживает —
    # оставляем False по умолчанию.
    smtp_use_tls: bool = False
    smtp_from_email: str = "noreply@showtail.local"
    smtp_from_name: str = "ShowTail"

    # APScheduler: включать ли встроенный планировщик. На dev удобно
    # держать выключенным, чтобы тестовые задания не запускались на
    # каждом старте. В prod включаем через .env.
    scheduler_enabled: bool = False

    # Имя topic exchange для событий платформы. То же, что и в очередях
    # воркера для подписок — обе стороны читают из этого поля.
    exchange_topic: str = "showtail.events"

    # --- Этап 14: Production-readiness ---
    # log_json=true — JSON-логи для ELK/Loki. false — человекочитаемый
    # текст для разработки (см. app/logging_config.py).
    log_json: bool = False
    # Уровень логирования: DEBUG/INFO/WARNING/ERROR.
    log_level: str = "INFO"
    # TTL кешированного ответа для idempotency-key (24 часа по умолчанию).
    # 24ч покрывает типичный сценарий "клиент перепосылает запрос после
    # сетевой ошибки"; дольше — не имеет смысла (запрос уже устарел).
    idempotency_ttl_seconds: int = 24 * 3600
    # Включает асинхронную (через RabbitMQ) обработку рекламных событий.
    # На dev обычно False (синхронный INSERT для простоты); на проде
    # под высоким трафиком включаем — батчинг в воркере сэкономит
    # round-trip'ы к PG.
    ad_events_async: bool = False

    # --- Доменно-специфичные политики ---
    # Список разрешённых Host-заголовков. Пустой = не проверяем (для dev).
    # В prod ставим ["api.showtail.example", "*.showtail.example"] —
    # защищает от Host header injection и host rebinding attacks.
    allowed_hosts: list[str] = []
    # Сети, которым доверяем X-Forwarded-For / X-Forwarded-Proto.
    # Список CIDR прокси-инфраструктуры (nginx, cloudflare).
    # Если задан — клиентский IP берём из X-Forwarded-For; иначе только
    # из request.client.host (защита от подделки IP анонимом).
    forwarded_allow_ips: list[str] = []
    # HSTS — Strict-Transport-Security. Включать только когда сайт уже
    # работает на HTTPS: иначе браузер не даст вернуться на http даже
    # для отладки.
    hsts_enabled: bool = False
    hsts_max_age_seconds: int = 63072000  # 2 года — рекомендация preload
    # CSP для API: 'default-src none' — мы JSON-API, никакого
    # содержимого, требующего ресурсы. frame-ancestors 'none' дублирует
    # X-Frame-Options для современных браузеров.
    csp_enabled: bool = False

    # bug_225 audit 2026-05-28: SQLAlchemy QueuePool по умолчанию
    # держит pool_size=5 и max_overflow=10 — потолок 15 одновременных
    # соединений. WebSocket-чат (per-message session, см. bug_205 fix)
    # + параллельные API-запросы + outbox-worker съедают это за
    # секунды под нагрузкой → QueuePool overflow и 30-секундный
    # timeout. 20+10 = 30 — комфортный потолок для среднего prod;
    # тюнится через .env под наблюдаемую нагрузку. PG max_connections
    # = 100 default; даже 3 инстанса по 30 = 90 < 100.
    db_pool_size: int = 20
    db_max_overflow: int = 10


settings = Settings()  # type: ignore
