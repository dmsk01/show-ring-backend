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


settings = Settings()  # type: ignore
