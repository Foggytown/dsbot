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

    # Instance identity. Two bots may run side by side (e.g. a test bot next to the
    # live one) as long as each uses its own name, database and token. The name selects
    # the lock and pid files, so `stop` in one deployment cannot kill the other.
    instance_name: str = Field(default="dsbot")

    # Optional explicit lock location. Defaults to var/<instance_name>.lock.
    lock_path: str = Field(default="")

    # Channels are bound from `channels.yaml` in the data directory, seeded once from
    # `forms/channels.yaml`. There is deliberately no environment-variable alternative:
    # one was documented here as an "escape hatch for panels without a shell", but nothing
    # read it, so it was a promise the bot could not keep - a configured channel would
    # simply never bind. The file in /data IS the panel-friendly route, because the panel
    # only keeps /data and the bot re-reads that file while running.

    # The form applied to a newly bound channel when the channels file names none.
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

    # How old a report may be before it is stored but never warned about.
    #
    # Without this the bot nags the channel about anything it happens to parse. That is
    # wrong in two ways: a message from months ago is not a mistake to fix now, and the
    # only reasons an old message gets re-evaluated are a reaction, an edit, or a
    # reconciliation pass - none of which mean "this report is late". Warnings are for
    # reports arriving now.
    #
    # It also bounds how much damage a bug in the scan or replay path can do: the worst
    # case becomes "some warnings were edited", never "the channel was spammed about
    # last winter". Set to 0 to warn about anything at any age, which is what a backfill
    # of a channel nobody has read yet may want.
    report_warn_max_age_hours: float = Field(default=24.0, ge=0)

    # Human to ping in warnings so reporters can ask about a disputed report.
    contact_user_id: int | None = Field(default=None)

    # Bot developer, pinged at the bottom of every generated report. Only an id: no
    # name, no links, nothing that could be used to identify or contact anyone off
    # Discord. Empty means no footer at all.
    developer_user_id: int | None = Field(default=None)

    # Minimum gap between Discord requests that read *who reacted*. Discord limits this
    # per channel while discord.py tracks it per message, so it must be paced by hand.
    # Lower it only if you never see 429s; a 429 costs several seconds.
    reaction_read_interval: float = Field(default=0.05)

    # Reuse stored reactors when the reaction set provably has not changed. On by default:
    # without it a repeat scan re-reads every report. Turn it off if a count ever looks
    # stale, and please report it.
    #
    # Wired through `count_channel_from_discord(use_cache=...)` from both the slash command
    # and the CLI. It used to be declared here and never read, so a stale-cache report had
    # no way to bypass the cache.
    reaction_cache: bool = Field(default=True)

    # Publish a channel's period automatically when its close time passes.
    #
    # Each channel stores its own close time (`/results set`), so this only enables the
    # watcher; nothing is scheduled here. Off means every report is run by hand.
    weekly_schedule_enabled: bool = Field(default=True)

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
        """The database URL to use, defaulting inside the state directory.

        A configured URL that would lose data is *overridden* rather than obeyed: see
        `database_location_problem`. Being right matters more than respecting a stale
        setting, and on a panel the settings are a web form the user edits, so refusing to
        start would leave the bot down until they noticed.
        """
        if self.database_url and self.database_location_problem() is None:
            return self.database_url
        return f"sqlite+aiosqlite:///{self.state_dir().as_posix()}/dsbot.db"

    def ignored_database_url(self) -> str | None:
        """The configured URL that was ignored because it would lose data."""
        if self.database_url and self.database_location_problem() is not None:
            return self.database_url
        return None

    def database_location_problem(self) -> str | None:
        """Why the configured database URL must not be used, or None.

        The panel keeps only its data directory and replaces everything else on upload, so
        a database outside it is deleted - taking every report, verdict and weekly result
        with it. A *relative* sqlite path always resolves into the checkout, so it is
        always wrong there. An absolute path elsewhere may be deliberate on a host that
        preserves it, so it is only warned about.
        """
        if not self.database_url:
            return None
        url = self.database_url
        if not url.startswith("sqlite"):
            # A real database server has its own storage; nothing to check here.
            return None

        raw = url.split("///", 1)[-1] if "///" in url else ""
        if not raw or ":memory:" in raw:
            return None

        if _sqlite_path(url) is None:
            # A relative path. This resolves against the working directory, which is the
            # checkout on a panel, so the file is deleted on the next upload.
            return (
                f"DSBOT_DATABASE_URL is a relative path ({raw}), which resolves inside the "
                "application checkout. A hosting panel deletes that on every upload, so "
                "all reports and verdicts would be lost. The default location is used "
                f"instead: {self.state_dir()}. Remove DSBOT_DATABASE_URL from the panel "
                "environment to silence this."
            )
        return None

    def database_location_warning(self) -> str | None:
        """An advisory for an absolute database path outside the data directory."""
        if not self.database_url or not self.database_url.startswith("sqlite"):
            return None
        path = _sqlite_path(self.database_url)
        if path is None:
            return None
        data_dir = self.state_dir().resolve()
        try:
            resolved = path.resolve()
        except OSError:
            return None
        if resolved == data_dir or data_dir in resolved.parents:
            return None
        return (
            f"the database is at {path}, outside the data directory {data_dir}. If this "
            "host wipes the checkout on deploy, that file and all history with it will be "
            "deleted. Move it into the data directory unless you know this path persists."
        )

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

    def intents(self) -> Any:
        import discord

        intents = discord.Intents.default()
        intents.message_content = True
        intents.reactions = True
        intents.members = self.enable_members_intent
        return intents


def get_settings() -> Settings:
    return Settings()


def _sqlite_path(url: str) -> Path | None:
    """The file path inside a sqlite URL, or None when there is no file.

    A POSIX absolute path must survive on a Windows dev machine: `/srv/db/x.db` is
    absolute there even though `Path("/srv/db/x.db").is_absolute()` is False on Windows.
    Without this, a deliberate absolute path was misread as relative.
    """
    if ":memory:" in url or url.endswith(":memory:"):
        return None
    raw = url.split("///", 1)[-1] if "///" in url else ""
    if not raw or raw in (":memory:", ""):
        return None
    candidate = Path(raw)
    if not candidate.is_absolute() and not raw.startswith("/"):
        return None
    return candidate
