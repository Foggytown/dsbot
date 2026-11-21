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

    # The channels configuration file.
    #
    # Defaults to <state dir>/channels.yaml, i.e. the panel's /data volume, so an admin
    # can edit it on the server and have it applied without a redeploy. The checked-in
    # `forms/channels.yaml` is only a seed: it is copied to /data once, on first start.
    channels_file: str = Field(default="")

    # Timezone used to interpret cron expressions.
    timezone: str = Field(default="Europe/Moscow")

    # Privileged intents, gated on what is actually implemented.
    # Message Content is required to read reports. Members is required to know who holds
    # a role, which is how the count knows who to report on: without it, people with no
    # reports are missing from the table instead of showing 0.
    enable_members_intent: bool = Field(default=True)

    # How long a report may wait for follow-up photos before being warned about.
    # Operator setting on purpose: it is a deployment-tuning value, not a per-channel
    # admin knob, so it lives in .env rather than in the database.
    report_group_window_seconds: float = Field(default=120.0)

    # Human to ping in warnings so reporters can ask about a disputed report.
    contact_user_id: int | None = Field(default=None)

    # Bot developer, pinged at the bottom of every generated report. Only an id: no
    # name, no links, nothing that could be used to identify or contact anyone off
    # Discord. Empty means no footer at all.
    developer_user_id: int | None = Field(default=None)

    # Minimum gap between Discord requests that read *who reacted*. Discord limits this
    # per channel while discord.py tracks it per message, so it must be paced by hand.
    # Lower it only if you never see 429s; a 429 costs several seconds.
    reaction_read_interval: float = Field(default=0.35)

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

    def resolved_channels_file(self) -> Path:
        """Where the served-channel list is read from: the writable data directory.

        It lives next to the database on purpose. The panel replaces the whole checkout on
        every upload but keeps /data, so a channel added by hand on the server survives
        only if the file is there. The copy in `forms/` is a seed, not the live file.
        """
        if self.channels_file:
            return Path(self.channels_file)
        return self.state_dir() / "channels.yaml"

    def seed_file(self) -> Path:
        """The checked-in template, copied into the data directory once."""
        return Path("forms/channels.yaml")

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
