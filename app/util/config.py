from functools import lru_cache

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "UpTimeTracker"
    app_env: str = "development"
    app_debug: bool = False
    log_level: str = "INFO"

    database_url: str
    direct_database_url: str | None = None
    redis_url: str = "redis://redis:6379/0"

    jwt_secret: SecretStr
    jwt_issuer: str = "uptimetracker-api"
    jwt_audience: str = "uptimetracker-client"
    access_token_ttl_seconds: int = 900
    refresh_token_ttl_days: int = 30

    brevo_smtp_host: str = "smtp-relay.brevo.com"
    brevo_smtp_port: int = 587
    brevo_smtp_login: str = ""
    brevo_smtp_key: SecretStr = SecretStr("")
    brevo_sender_email: str = ""
    brevo_sender_name: str = ""
    # Brevo HTTP API key for alert emails (app/services/email). Optional so the
    # app still starts without it.
    brevo_api_key: SecretStr = SecretStr("")
    email_code_ttl_minutes: int = 10
    app_frontend_url: str = "http://localhost:3000"
    invitation_ttl_hours: int = 48

    # Webhooks
    # DEV ONLY: allows http:// and private/local targets. Must stay false in production.
    webhook_allow_private_targets: bool = False
    # DEV ONLY: lets health checks reach localhost and private networks.
    # Must stay false in production: otherwise any customer could make our
    # servers fetch internal addresses (cloud metadata, admin panels).
    monitor_allow_private_targets: bool = False

    # Notification channels: Fernet keys, comma-separated. The FIRST key encrypts;
    # every key can decrypt, so keys can be rotated.
    channel_encryption_keys: SecretStr = SecretStr("")

    sentry_dsn: str | None = None
    sentry_environment: str = "development"

    @field_validator("jwt_secret")
    @classmethod
    def validate_jwt_secret(cls, value: SecretStr) -> SecretStr:
        secret = value.get_secret_value()
        if len(secret.encode("utf-8")) < 32 or secret == "change-me":
            raise ValueError("JWT_SECRET must be a random value of at least 32 bytes")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # Values are loaded from environment.


settings = get_settings()
