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


settings = Settings()  # type: ignore
