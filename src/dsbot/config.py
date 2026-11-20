"""Configuration, loaded from environment or .env.

Every value here is infrastructure, not domain policy. Domain policy (quotas, forms,
rank ladders, message templates) lives in the database so a Discord admin can change
it without a redeploy.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

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

    # Where persistent state lives. Hosting panels keep only one directory across
    # redeploys, so everything writable must be under it: the database, the lock, the
    # pid file. `DATA_DIR` is the panel convention; DSBOT_DATA_DIR overrides it.
    data_dir: str = Field(default="")

    database_url: str = Field(default="")
    sql_echo: bool = Field(default=False)

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(default="INFO")
    log_format: Literal["console", "json"] = Field(default="console")

    default_timezone: str = Field(default="Europe/Moscow")
    default_locale: Literal["en", "ru"] = Field(default="ru")

    # Discord REST concurrency ceiling per host, in requests/second. Backfill uses
    # this to stay inside the global bucket without triggering 429s.
    rest_requests_per_second: float = Field(default=4.0)

    # Instance identity. Two bots may run side by side (e.g. a test bot next to the
    # live one) as long as each uses its own name, database and token. The name selects
    # the lock and pid files, so `stop` in one deployment cannot kill the other.
    instance_name: str = Field(default="dsbot")

    # Optional explicit lock location. Defaults to var/<instance_name>.lock.
    lock_path: str = Field(default="")

    # Channels to bind at startup, as "guild_id:channel_id" pairs separated by commas.
    #
    # Prefer `forms/channels.yaml`, which lives in git. This variable is the escape hatch
    # for panels that provide no shell and cannot use the file.
    #
    #   DSBOT_AUTO_BIND_CHANNELS=1209516013779943544:1573045809774067812
    auto_bind_channels: str = Field(default="")

    # The form applied to channels bound via AUTO_BIND_CHANNELS.
    auto_bind_form: str = Field(default="forms/training.yaml")

    # The channels configuration file. Read at startup and applied to the database.
    channels_file: str = Field(default="forms/channels.yaml")

    # Timezone used to interpret cron expressions.
    timezone: str = Field(default="Europe/Moscow")

    # Privileged intents, gated on what is actually implemented.
    # Message Content is required to read reports. Members is only needed once rank
    # resolution exists, so it stays off until then and avoids a needless portal step.
    enable_members_intent: bool = Field(default=False)

    # How long a report may wait for follow-up photos before being warned about.
    # Operator setting on purpose: it is a deployment-tuning value, not a per-channel
    # admin knob, so it lives in .env rather than in the database.
    report_group_window_seconds: float = Field(default=120.0)

    # Human to ping in warnings so reporters can ask about a disputed report.
    contact_user_id: int | None = Field(default=None)

    # DEV ONLY. Accept messages authored by bots, so the live gateway path can be
    # exercised by posting a test message as the bot itself. Never enable in production.
    dev_accept_bot_messages: bool = Field(default=False)

    @property
    def is_sqlite(self) -> bool:
        return self.resolved_database_url.startswith("sqlite")

    def state_dir(self) -> Path:
        """Directory for everything that must survive a redeploy.

        Resolution order:
          1. DSBOT_DATA_DIR
          2. DATA_DIR environment variable
          3. an existing panel directory: /data or /app/data
          4. ./var (local development)

        The panel keeps only its data directory across restarts, so the database and
        lock must live there or all history is lost on every redeploy. Auto-detecting
        the common panel paths means a deployment works without any extra configuration.
        """
        if self.data_dir:
            return Path(self.data_dir)
        for variable in ("DATA_DIR", "DSBOT_DATA_DIR"):
            value = os.getenv(variable, "").strip()
            if value:
                return Path(value)
        for candidate in (Path("/data"), Path("/app/data")):
            if candidate.is_dir():
                return candidate
        return Path("var")

    @property
    def resolved_database_url(self) -> str:
        """Database URL, defaulting inside the state directory."""
        if self.database_url:
            return self.database_url
        return f"sqlite+aiosqlite:///{self.state_dir().as_posix()}/dsbot.db"

    def lock_file(self) -> Path:
        """Lock file for this instance.

        Distinct per `instance_name`, so a test bot and a live bot never block each
        other and `stop` only ever targets its own deployment.
        """
        if self.lock_path:
            return Path(self.lock_path)
        return self.state_dir() / f"{self.instance_name}.lock"

    def pid_file(self) -> Path:
        return self.lock_file().with_suffix(".pid")

    def auto_bind_pairs(self) -> list[tuple[int, int]]:
        """Parse `DSBOT_AUTO_BIND_CHANNELS` into (guild_id, channel_id) pairs.

        Malformed entries are skipped rather than crashing the bot: a typo in one pair
        must not prevent the others from binding.
        """
        pairs: list[tuple[int, int]] = []
        for chunk in self.auto_bind_channels.replace(";", ",").split(","):
            entry = chunk.strip()
            if not entry:
                continue
            separator = ":" if ":" in entry else "/"
            left, _, right = entry.partition(separator)
            try:
                pairs.append((int(left.strip()), int(right.strip())))
            except ValueError:
                continue
        return pairs

    def intents(self) -> Any:
        import discord

        intents = discord.Intents.default()
        intents.message_content = True
        intents.reactions = True
        intents.members = self.enable_members_intent
        return intents


def get_settings() -> Settings:
    return Settings()
