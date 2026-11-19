from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "pulseMonitor"
    app_env: str = "development"
    app_debug: bool = False
    log_level: str = "INFO"

    database_url: str
    direct_database_url: str | None = None

    redis_url: str = "redis://localhost:6379/0"

    sentry_dsn: str | None = None
    sentry_environment: str = "development"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
