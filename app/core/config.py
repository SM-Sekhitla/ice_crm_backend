from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, EmailStr, Field, SecretStr, TypeAdapter, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    app_name: str = "ICE CRM API"
    app_host: str = "0.0.0.0"
    app_port: int = Field(5002, ge=1, le=65535, validation_alias=AliasChoices("APP_PORT", "BACKEND_PORT"))
    web_concurrency: int = Field(1, ge=1, le=32)
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
    backend_log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    default_platform_admin_email: str = ""
    default_platform_admin_password: SecretStr = Field(default_factory=lambda: SecretStr(""), repr=False)
    default_platform_admin_username: str = Field(default="Platform Admin", max_length=200)

    @field_validator("default_platform_admin_email", mode="before")
    @classmethod
    def normalize_admin_email(cls, value):
        value = value.strip() if isinstance(value, str) else value
        return str(TypeAdapter(EmailStr).validate_python(value)).lower() if value else ""

    @field_validator("default_platform_admin_password")
    @classmethod
    def validate_admin_password(cls, value):
        password = value.get_secret_value()
        if password and not 12 <= len(password) <= 128:
            raise ValueError("DEFAULT_PLATFORM_ADMIN_PASSWORD must contain 12–128 characters")
        return value

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True
    smtp_ssl: bool = False
    frontend_base_url: str = "http://localhost:5173"
    invitation_expiry_hours: int = Field(24, ge=1, le=168)
    smtp_from_email: str = ""
    smtp_from_name: str = "ICE Business Development"
    email_delivery_enabled: bool = False

    @field_validator("frontend_base_url")
    @classmethod
    def validate_frontend_url(cls, value):
        from urllib.parse import urlsplit

        value = value.strip().rstrip("/")
        parsed = urlsplit(value)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "FRONTEND_BASE_URL must be an HTTP(S) frontend URL without credentials, query or fragment"
            )
        return value

    @field_validator("smtp_from_email")
    @classmethod
    def validate_smtp_from_email(cls, value):
        return str(TypeAdapter(EmailStr).validate_python(value.strip())).lower() if value.strip() else ""

    email_provider: Literal["smtp", "brevo"] = "smtp"
    brevo_api_key: str = ""
    brevo_from_email: str = ""
    brevo_from_name: str = "ICE Business Development"

    @field_validator("email_provider", mode="before")
    @classmethod
    def normalize_provider(cls, value):
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("brevo_from_email")
    @classmethod
    def validate_brevo_email(cls, value):
        return str(TypeAdapter(EmailStr).validate_python(value.strip())).lower() if value.strip() else ""

    @field_validator("brevo_from_name", "smtp_from_name")
    @classmethod
    def validate_brevo_name(cls, value):
        if not value.strip() or len(value) > 150 or "\r" in value or "\n" in value:
            raise ValueError("Sender display name must be a single-line name of 1–150 characters")
        return value.strip()

    @model_validator(mode="after")
    def validate_configuration(self):
        if self.secret_key.startswith("replace-this"):
            raise ValueError("Generate a unique SECRET_KEY before starting the API")
        if not self.database_url.startswith("postgresql+asyncpg://"):
            raise ValueError("DATABASE_URL must use postgresql+asyncpg")
        if self.environment == "production" and ("*" in self.cors_origins or "*" in self.allowed_hosts):
            raise ValueError("Production requires explicit origins and hosts")
        if self.email_delivery_enabled and self.email_provider == "smtp" and not self.smtp_host:
            raise ValueError("SMTP_HOST is required when delivery is enabled")
        if self.email_delivery_enabled and self.email_provider == "brevo":
            if not self.brevo_api_key.strip() or not self.brevo_from_email:
                raise ValueError("Brevo delivery requires BREVO_API_KEY and BREVO_FROM_EMAIL")
        if self.email_provider == "smtp" and self.smtp_ssl and self.smtp_starttls:
            raise ValueError("Use SMTP_SSL or SMTP_STARTTLS, not both")
        return self


@lru_cache
def get_settings():
    return Settings()
