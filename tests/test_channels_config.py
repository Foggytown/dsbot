"""Channels as configuration: forms/channels.yaml is applied to the database.

The file lives in git, so a channel configured locally is deployed by pushing. These
tests pin the properties that make that safe: idempotency, respect for a deliberate
pause, and no destruction when an entry is removed from the file.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from dsbot.config import Settings
from dsbot.modules.reports.channels import (
    ChannelsFile,
    ChannelSpec,
    channels_problems,
    load_channels_file,
)
from dsbot.modules.reports.channels_sync import FORM_FILES, sync_channels
from dsbot.storage import repos
from dsbot.storage.db import Database

GUILD = 900
CHANNEL = 500


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(
        Settings(_env_file=None, database_url=f"sqlite+aiosqlite:///{tmp_path / 'ch.db'}")  # type: ignore[call-arg]
    )
    await database.create_all()
    yield database
    await database.dispose()


def spec(*channels: ChannelSpec) -> ChannelsFile:
    return ChannelsFile(channels=list(channels))


def entry(**overrides: object) -> ChannelSpec:
    base = {"guild": GUILD, "channel": CHANNEL, "form": "training_report"}
    return ChannelSpec.model_validate({**base, **overrides})


# --- file parsing ----------------------------------------------------------


def test_shipped_file_loads() -> None:
    spec = load_channels_file(Path("forms/channels.yaml"))
    assert len(spec.channels) >= 1
    for item in spec.channels:
        assert item.guild > 0
        assert item.channel > 0


def test_missing_file_means_no_channels(tmp_path: Path) -> None:
    """A fresh checkout must start and say so, not crash."""
    spec = load_channels_file(tmp_path / "nope.yaml")
    assert spec.channels == []


def test_duplicate_channel_is_rejected() -> None:
    with pytest.raises(ValueError, match="listed twice"):
        spec(entry(), entry(form="other"))


def test_unknown_locale_is_rejected() -> None:
    with pytest.raises(ValueError, match="locale"):
        spec(entry(locale="de"))


def test_rank_guild_defaults_to_guild() -> None:
    assert entry().rank_source_guild == GUILD
    assert entry(rank_guild=999).rank_source_guild == 999


def test_empty_file_is_reported_as_a_problem() -> None:
    assert channels_problems(spec())


# --- applying to the database ---------------------------------------------


async def test_creates_missing_binding(db: Database) -> None:
    async with db.write_session() as session:
        result = await sync_channels(session, spec(entry()))

    assert result.created == [CHANNEL]
    async with db.session() as session:
        binding = await repos.get_binding_by_channel(session, CHANNEL)
    assert binding is not None
    assert binding.guild_id == GUILD


async def test_second_apply_changes_nothing(db: Database) -> None:
    """Idempotency is what makes redeploying safe."""
    async with db.write_session() as session:
        await sync_channels(session, spec(entry()))
    async with db.write_session() as session:
        result = await sync_channels(session, spec(entry()))

    assert result.created == []
    assert result.updated == []
    assert result.unchanged == [CHANNEL]


async def test_paused_entry_creates_a_disabled_binding(db: Database) -> None:
    async with db.write_session() as session:
        result = await sync_channels(session, spec(entry(enabled=False)))

    assert result.created == [CHANNEL]
    async with db.session() as session:
        assert await repos.get_binding_by_channel(session, CHANNEL) is None
        assert await repos.get_any_binding(session, CHANNEL) is not None


async def test_file_can_pause_an_active_channel(db: Database) -> None:
    async with db.write_session() as session:
        await sync_channels(session, spec(entry()))
    async with db.write_session() as session:
        result = await sync_channels(session, spec(entry(enabled=False)))

    assert result.paused == [CHANNEL]
    async with db.session() as session:
        binding = await repos.get_any_binding(session, CHANNEL)
    assert binding is not None
    assert binding.enabled is False


async def test_file_can_resume_a_paused_channel(db: Database) -> None:
    async with db.write_session() as session:
        await sync_channels(session, spec(entry(enabled=False)))
    async with db.write_session() as session:
        await sync_channels(session, spec(entry()))

    async with db.session() as session:
        assert await repos.get_binding_by_channel(session, CHANNEL) is not None


async def test_rank_guild_is_stored(db: Database) -> None:
    """Approvals resolve against the specialization server, not the report server."""
    async with db.write_session() as session:
        await sync_channels(session, spec(entry(rank_guild=999)))

    async with db.session() as session:
        binding = await repos.get_binding_by_channel(session, CHANNEL)
    assert binding is not None
    assert binding.rank_source_guild_id == 999


async def test_removing_an_entry_keeps_its_history(db: Database) -> None:
    """Dropping a channel from the file must not delete anything."""
    async with db.write_session() as session:
        await sync_channels(session, spec(entry()))
    async with db.write_session() as session:
        await sync_channels(session, spec())

    async with db.session() as session:
        binding = await repos.get_any_binding(session, CHANNEL)
    assert binding is not None


async def test_locale_change_is_applied(db: Database) -> None:
    async with db.write_session() as session:
        await sync_channels(session, spec(entry()))
    async with db.write_session() as session:
        await sync_channels(session, spec(entry(locale="en")))

    async with db.session() as session:
        binding = await repos.get_binding_by_channel(session, CHANNEL)
    assert binding is not None
    assert binding.locale == "en"


async def test_unknown_form_fails_loudly(db: Database) -> None:
    with pytest.raises(FileNotFoundError, match="not found"):
        async with db.write_session() as session:
            await sync_channels(session, spec(entry(form="does_not_exist")))


def test_shipped_forms_are_resolvable() -> None:
    """Every form key referenced by the shipped file must resolve."""
    for key, path in FORM_FILES.items():
        assert path.is_file(), f"{key} -> {path} is missing"
