"""Channels as a configuration file.

Forms and ladders are YAML, and the list of served channels belongs with them: it is
deployment configuration, not runtime data. Keeping it in git means a channel you set up
locally is deployed by pushing, with no shell on the server.

What stays in the server database: messages, reports, reactions, verdicts, weekly
results. Nothing here changes while the bot runs, so there is no "merging" to do.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator

DEFAULT_CHANNELS_FILE = Path("forms/channels.yaml")


class ChannelSpec(BaseModel):
    """One channel to serve."""

    guild: int
    channel: int
    form: str = "training_report"
    # Which guild's role ladder governs approvals. Defaults to `guild`.
    rank_guild: int | None = None
    locale: str = "ru"
    # Disabled channels stay bound and keep their history; they just stop being served.
    enabled: bool = True

    @property
    def rank_source_guild(self) -> int:
        return self.rank_guild if self.rank_guild is not None else self.guild


class ChannelsFile(BaseModel):
    channels: list[ChannelSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _no_duplicates(self) -> ChannelsFile:
        seen: set[int] = set()
        for entry in self.channels:
            if entry.channel in seen:
                raise ValueError(
                    f"channel {entry.channel} is listed twice; "
                    "a channel can only be bound to one form"
                )
            seen.add(entry.channel)
        return self

    @model_validator(mode="after")
    def _locales_known(self) -> ChannelsFile:
        allowed = {"ru", "en"}
        for entry in self.channels:
            if entry.locale not in allowed:
                raise ValueError(
                    f"channel {entry.channel}: locale {entry.locale!r} "
                    f"is not one of {sorted(allowed)}"
                )
        return self


@dataclass(slots=True)
class ChannelsSyncResult:
    created: list[int] = field(default_factory=list)
    updated: list[int] = field(default_factory=list)
    paused: list[int] = field(default_factory=list)
    unchanged: list[int] = field(default_factory=list)


def load_channels_file(path: Path = DEFAULT_CHANNELS_FILE) -> ChannelsFile:
    """Read the channels file. A missing file means "no channels", not an error.

    That matters on a fresh deployment: the bot should start and say plainly that
    nothing is bound rather than crash.
    """
    if not path.is_file():
        return ChannelsFile(channels=[])
    data = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return ChannelsFile.model_validate(data)


def channels_problems(spec: ChannelsFile) -> list[str]:
    """Catch the mistakes an admin actually makes."""
    problems: list[str] = []
    if not spec.channels:
        problems.append("no channels listed, so the bot would serve nothing")
    for entry in spec.channels:
        if entry.channel <= 0 or entry.guild <= 0:
            problems.append(f"channel {entry.channel}: guild and channel must be positive ids")
    return problems
