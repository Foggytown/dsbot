"""Configuration, loaded from environment or .env.

Every value here is infrastructure, not domain policy. Domain policy (quotas, forms,
rank ladders, message templates) lives in the database so a Discord admin can change
it without a redeploy.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DSBOT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    discord_token: str = Field(default="")

    database_url: str = Field(default="sqlite+aiosqlite:///./var/dsbot.db")
    sql_echo: bool = Field(default=False)

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(default="INFO")
    log_format: Literal["console", "json"] = Field(default="console")

    default_timezone: str = Field(default="Europe/Moscow")
    default_locale: Literal["en", "ru"] = Field(default="ru")

    # Discord REST concurrency ceiling per host, in requests/second. Backfill uses
    # this to stay inside the global bucket without triggering 429s.
    rest_requests_per_second: float = Field(default=4.0)

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


def get_settings() -> Settings:
    return Settings()
