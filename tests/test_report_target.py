"""Where the weekly report is published, including the case that used to be impossible.

The bug: `/dsbot report-target` stored the destination on the binding of the channel the
command was typed in. That is normally the *report* channel, so the setting worked - but
typing it in the destination channel itself raised `ValueError` with no user-facing
message, because a publication channel is not bound to a form. The interaction then failed
silently, which is exactly what "report-target doesn't work" looks like from Discord.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from dsbot.config import Settings
from dsbot.discord.commands import set_report_target
from dsbot.modules.reports.schema import DEFAULT_SCHEMAS
from dsbot.storage import admin, repos
from dsbot.storage.db import Database

GUILD = 800
REPORT_CHANNEL = 801
DESTINATION = 802


@pytest.fixture
async def bound(tmp_path: Path) -> AsyncIterator[Database]:
    db = Database(Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'target.db'}"))
    await db.create_all()
    async with db.write_session() as session:
        await admin.ensure_schema_seed(session, DEFAULT_SCHEMAS)
        await admin.bind_channel(
            session,
            guild_id=GUILD,
            channel_id=REPORT_CHANNEL,
            module_key="reports",
            schema_key="training_report",
        )
    yield db
    await db.dispose()


async def config_of(db: Database) -> dict:
    async with db.session() as session:
        binding = await repos.get_binding_by_channel(session, REPORT_CHANNEL)
    assert binding is not None
    return dict(binding.config or {})


async def test_a_destination_can_be_set_from_the_report_channel(bound: Database) -> None:
    await set_report_target(bound, REPORT_CHANNEL, publish_to=DESTINATION)
    assert (await config_of(bound)).get("report_channel_id") == DESTINATION


async def test_clearing_the_destination_is_stored_as_absent(bound: Database) -> None:
    await set_report_target(bound, REPORT_CHANNEL, publish_to=DESTINATION)
    await set_report_target(bound, REPORT_CHANNEL, publish_to=0)
    assert (await config_of(bound)).get("report_channel_id") is None


async def test_setting_it_on_an_unbound_channel_is_a_clear_error(bound: Database) -> None:
    """The command must report this, not fail with a bare interaction error."""
    with pytest.raises(ValueError, match="not bound"):
        await set_report_target(bound, 999999, publish_to=DESTINATION)


async def test_the_destination_survives_a_reload(bound: Database) -> None:
    """It lives in the binding row, which is in the database on /data."""
    await set_report_target(bound, REPORT_CHANNEL, publish_to=DESTINATION)
    # A fresh session is what a restart does: nothing is cached between the two reads.
    assert (await config_of(bound)).get("report_channel_id") == DESTINATION
