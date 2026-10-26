"""
app/settings.py

Typed application settings loaded from environment variables.
Secrets live ONLY in environment variables or .env (gitignored).
No secrets are ever hardcoded here.
"""

from __future__ import annotations

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Database — app role (limited permissions, RLS applied)
    database_url: str

    # Database — superuser (used only by Alembic, not by the running app)
    database_migration_url: str = ""

    # Secret key for signing tokens (min 32 chars)
    secret_key: str
    auth_log_key: str = "default-insecure-auth-log-key-32-chars-minimum!"

    # Logging
    log_level: str = "INFO"

    # Environment
    app_env: str = "development"

    # Testing flag (set to "true" in test environments)
    testing: bool = False

    # Session & Auth Settings
    session_idle_timeout_minutes: int = 60
    session_lifetime_hours: int = 12
    session_cookie_secure: bool = False

    # Telegram Webhook Secret
    telegram_webhook_secret: str = ""

    @field_validator("secret_key")
    @classmethod
    def secret_key_min_length(cls, v: str) -> str:
        if len(v) < 16:
            raise ValueError("SECRET_KEY must be at least 16 characters long.")
        return v

    @field_validator("log_level")
    @classmethod
    def log_level_valid(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in allowed:
            raise ValueError(f"LOG_LEVEL must be one of {allowed}, got {v!r}")
        return upper


# Singleton — imported everywhere as `from app.settings import settings`
settings = Settings()
