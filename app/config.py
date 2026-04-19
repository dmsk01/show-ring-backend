from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")
    rabbitmq_url: str = "amqp://guest:guest@localhost/"
    exchange_events: str = "events"
    database_url: str


settings = Settings()  # type: ignore
