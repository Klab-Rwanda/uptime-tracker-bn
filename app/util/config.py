from functools import lru_cache

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str
    redis_url: str
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
    email_code_ttl_minutes: int = 10

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
