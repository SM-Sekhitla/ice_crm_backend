from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    app_name: str = "ICE CRM API"
    environment: str = "development"
    database_url: str = "postgresql+asyncpg://crm:crm@localhost:5432/crm"
    secret_key: str = Field(min_length=32)
    token_issuer: str = "ice-crm"
    access_token_minutes: int = Field(15, ge=1, le=60)
    refresh_token_days: int = Field(7, ge=1, le=30)
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    allowed_hosts: list[str] = ["localhost", "127.0.0.1", "testserver"]
    request_max_bytes: int = 2_000_000
    auth_rate_limit: int = 20
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True
    smtp_ssl: bool = False
    email_delivery_enabled: bool = False

    @model_validator(mode="after")
    def validate_configuration(self):
        if self.secret_key.startswith("replace-this"):
            raise ValueError("Generate a unique SECRET_KEY before starting the API")
        if not self.database_url.startswith("postgresql+asyncpg://"):
            raise ValueError("DATABASE_URL must use postgresql+asyncpg")
        if self.environment == "production" and ("*" in self.cors_origins or "*" in self.allowed_hosts):
            raise ValueError("Production requires explicit origins and hosts")
        if self.email_delivery_enabled and not self.smtp_host:
            raise ValueError("SMTP_HOST is required when delivery is enabled")
        if self.smtp_ssl and self.smtp_starttls:
            raise ValueError("Use SMTP_SSL or SMTP_STARTTLS, not both")
        return self


@lru_cache
def get_settings():
    return Settings()
