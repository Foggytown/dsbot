"""Settings changed from Discord must survive a redeploy.

The panel keeps only /data and replaces the checkout on every upload, so anything an admin
tunes with a slash command has to live in the database. A setting kept in memory, or in a
file inside the checkout, would silently reset — and nobody would notice until a report
looked wrong weeks later.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from dsbot.config import Settings
from dsbot.discord.commands import set_hidden_tiers, set_report_target
from dsbot.modules.reports.channels import ChannelsFile, ChannelSpec
from dsbot.modules.reports.channels_sync import sync_channels
from dsbot.storage import admin, repos
from dsbot.storage.db import Database

GUILD = 900
CHANNEL = 500


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(
        Settings(_env_file=None, database_url=f"sqlite+aiosqlite:///{tmp_path / 's.db'}")  # type: ignore[call-arg]
    )
    await database.prepare()
    async with database.write_session() as session:
        await admin.bind_channel(session, guild_id=GUILD, channel_id=CHANNEL, module_key="reports")
    yield database
    await database.dispose()


async def config_of(db: Database) -> dict[str, object]:
    async with db.session() as session:
        binding = await repos.get_binding_by_channel(session, CHANNEL)
        assert binding is not None
        return dict(binding.config or {})


async def test_hidden_tiers_are_stored_in_the_binding(db: Database) -> None:
    await set_hidden_tiers(db, CHANNEL, hide=["Исключение"], show=[])
    assert (await config_of(db)).get("hidden_tiers") == ["Исключение"]


async def test_hiding_twice_does_not_duplicate(db: Database) -> None:
    await set_hidden_tiers(db, CHANNEL, hide=["A"], show=[])
    await set_hidden_tiers(db, CHANNEL, hide=["A"], show=[])
    assert (await config_of(db)).get("hidden_tiers") == ["A"]


async def test_showing_a_tier_removes_it(db: Database) -> None:
    await set_hidden_tiers(db, CHANNEL, hide=["A", "B"], show=[])
    await set_hidden_tiers(db, CHANNEL, hide=[], show=["a"])
    assert (await config_of(db)).get("hidden_tiers") == ["B"]


async def test_publish_target_is_stored(db: Database) -> None:
    await set_report_target(db, CHANNEL, publish_to=123)
    assert (await config_of(db)).get("report_channel_id") == 123


async def test_publish_target_can_be_cleared(db: Database) -> None:
    await set_report_target(db, CHANNEL, publish_to=123)
    await set_report_target(db, CHANNEL, publish_to=0)
    assert (await config_of(db)).get("report_channel_id") is None


async def test_an_old_title_setting_is_removed(db: Database) -> None:
    """The title was dropped from the product; a stale key must not linger."""
    async with db.write_session() as session:
        binding = await repos.get_binding_by_channel(session, CHANNEL)
        assert binding is not None
        binding.config = {"report_title": "старый заголовок"}
    await set_report_target(db, CHANNEL, publish_to=5)
    assert "report_title" not in (await config_of(db))


async def test_channels_sync_keeps_admin_settings(db: Database) -> None:
    """A redeploy re-applies the channels file; it must not reset what an admin chose."""
    await set_hidden_tiers(db, CHANNEL, hide=["Исключение"], show=[])
    await set_report_target(db, CHANNEL, publish_to=42)

    async with db.write_session() as session:
        await sync_channels(
            session, ChannelsFile(channels=[ChannelSpec(guild=GUILD, channel=CHANNEL)])
        )

    config = await config_of(db)
    assert config.get("hidden_tiers") == ["Исключение"]
    assert config.get("report_channel_id") == 42


async def test_hiding_on_an_unbound_channel_is_refused(db: Database) -> None:
    with pytest.raises(ValueError):
        await set_hidden_tiers(db, 999999, hide=["A"], show=[])
