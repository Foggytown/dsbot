"""Channels as a configuration file.

Forms, ladders and served channels are all configuration, so they live together in git
and are applied the same way: on startup, automatically, with no manual step. A channel
entry names everything that governs it:

    channels:
      - guild: 1274460259661910149
        channel: 1274460260941434901
        form: training_report          # which form to parse
        rank_guild: 1274460259661910149 # which server holds the roles
        review_ladder: review           # who may approve
        quota_ladder: quota             # who owes reports, and how many

That last pair is the answer to "which ladder applies here": it is written per channel,
not inferred from a single global ladder. Apply the file and the bot knows both, for that
channel only.

What stays in the server database: messages, reports, reactions, verdicts, weekly
results. Nothing here changes while the bot runs, so there is no "merging" to do.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator

DEFAULT_CHANNELS_FILE = Path("forms/channels.yaml")


def seed_channels_file(target: Path, template: Path = DEFAULT_CHANNELS_FILE) -> bool:
    """Copy the checked-in template into the data directory, once.

    The live file must live where the panel preserves data (/data), because the checkout
    is replaced on every deploy. Seeding only when the target is missing means an admin's
    edits are never overwritten by a redeploy: `forms/channels.yaml` is a starting point,
    not the source of truth once deployed.
    """
    if target.exists():
        return False
    if not template.is_file():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(template.read_text(encoding="utf-8-sig"), encoding="utf-8")
    return True


def file_signature(path: Path) -> tuple[int, int] | None:
    """Cheap change detector: (mtime, size). None when the file is gone.

    Used to notice an edit without polling the whole file or trusting a filesystem
    watcher, which behaves differently on Windows and inside a container.
    """
    try:
        stat = path.stat()
    except OSError:
        return None
    return (int(stat.st_mtime_ns), stat.st_size)


class ChannelSpec(BaseModel):
    """One channel to serve, and everything that governs it."""

    guild: int
    channel: int
    form: str = "training_report"

    # Which guild's roles decide authority. Defaults to `guild`.
    rank_guild: int | None = None

    # Ladder keys, resolved inside the rank guild. Naming them here is what makes the
    # hierarchy per-channel: two channels can point at different ladders, and either may
    # be omitted (approvals only, or counting only).
    review_ladder: str | None = None
    quota_ladder: str | None = None

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
    ladders: list[str] = field(default_factory=list)
    forms: list[str] = field(default_factory=list)


def load_channels_file(path: Path = DEFAULT_CHANNELS_FILE) -> ChannelsFile:
    """Read the channels file. A missing file means "no channels", not an error.

    That matters on a fresh deployment: the bot should start and say plainly that
    nothing is bound rather than crash.
    """
    if not path.is_file():
        return ChannelsFile(channels=[])
    data: object = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
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
