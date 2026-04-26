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


settings = Settings()  # type: ignore
