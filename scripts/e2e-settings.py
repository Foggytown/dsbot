"""Proves that settings changed from Discord survive a restart.

The panel keeps only /data: the checkout is replaced on every deploy. Anything an admin
sets with a slash command must therefore live in the database, which does live in /data.
This exercises the exact write-then-reopen cycle a redeploy performs.

    uv run python scripts/e2e-settings.py
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from dsbot.config import Settings
from dsbot.discord.commands import set_hidden_tiers, set_report_target
from dsbot.storage import admin
from dsbot.storage.db import Database
from dsbot.storage.models import ChannelBinding

GUILD = 900
CHANNEL = 500


async def main() -> None:
    data_dir = Path(tempfile.mkdtemp())  # stands in for /data
    url = f"sqlite+aiosqlite:///{(data_dir / 'dsbot.db').as_posix()}"

    # --- first run: an admin sets things from Discord ---
    db = Database(Settings(database_url=url))
    await db.prepare()
    async with db.write_session() as session:
        binding = await admin.bind_channel(
            session,
            guild_id=GUILD,
            channel_id=CHANNEL,
            module_key="reports",
        )
        binding_id = binding.id
    await set_report_target(db, CHANNEL, publish_to=123456789)
    await set_hidden_tiers(db, CHANNEL, hide=["Исключение", "Instructor"], show=[])
    await db.dispose()
    print("set from Discord: publish_to=123456789, hidden=[Исключение, Instructor]")

    # --- redeploy: new process, same /data ---
    db = Database(Settings(database_url=url))
    await db.prepare()
    async with db.session() as session:
        row = await session.get(ChannelBinding, binding_id)
        config = dict(row.config or {}) if row is not None else {}
    await db.dispose()

    print(f"after restart:    publish_to={config.get('report_channel_id')}")
    print(f"                  hidden={config.get('hidden_tiers')}")

    assert config.get("report_channel_id") == 123456789, "publish target lost"
    assert config.get("hidden_tiers") == ["Исключение", "Instructor"], "hidden tiers lost"

    # --- a reload from git must not wipe them either ---
    db = Database(Settings(database_url=url))
    await db.prepare()
    from dsbot.modules.reports.channels import ChannelsFile, ChannelSpec
    from dsbot.modules.reports.channels_sync import sync_channels

    async with db.write_session() as session:
        await sync_channels(
            session,
            ChannelsFile(channels=[ChannelSpec(guild=GUILD, channel=CHANNEL)]),
        )
    async with db.session() as session:
        row = await session.get(ChannelBinding, binding_id)
        after_sync = dict(row.config or {}) if row is not None else {}
    await db.dispose()

    print(f"after channels-apply: publish_to={after_sync.get('report_channel_id')}")
    print(f"                      hidden={after_sync.get('hidden_tiers')}")
    assert after_sync.get("report_channel_id") == 123456789, "sync wiped the publish target"
    assert after_sync.get("hidden_tiers") == ["Исключение", "Instructor"], "sync wiped hidden"

    print("\nOK: settings made in Discord persist in /data and survive both a restart")
    print("    and a channels-apply from git")


if __name__ == "__main__":
    asyncio.run(main())
