"""Checks that every slash command group builds and registers without Discord.

Discord rejects a malformed command tree at sync time, which on a live bot means the
commands silently never appear. Building the tree here catches that before deployment.

    uv run python scripts/check-commands.py
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import discord
from discord import app_commands

from dsbot.config import Settings
from dsbot.discord.commands import AdminCommands
from dsbot.discord.count_commands import CountCommands
from dsbot.storage.db import Database


async def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    settings = Settings(database_url=f"sqlite+aiosqlite:///{(tmp / 'c.db').as_posix()}")
    db = Database(settings)
    try:
        await db.prepare()

        admin = AdminCommands(db)
        count = CountCommands(db, None, "Europe/Moscow")
        print("admin commands:", sorted(c.name for c in admin.commands))
        print("count commands:", sorted(c.name for c in count.commands))

        tree = app_commands.CommandTree(discord.Client(intents=discord.Intents.none()))
        tree.add_command(admin)
        tree.add_command(count)

        checked = 0
        for group in tree.get_commands():
            if isinstance(group, app_commands.Group):
                for sub in group.commands:
                    assert sub.description, f"{group.name}/{sub.name} has no description"
                    assert sub.name == sub.name.lower(), f"{sub.name} must be lowercase"
                    checked += 1
        print(f"tree root: {[g.name for g in tree.get_commands()]}")
        print(f"{checked} subcommands have descriptions and valid names")
        print("\nOK: the command tree is valid")
    finally:
        # Dispose before the directory goes away: Windows keeps the handle open and the
        # cleanup error would otherwise hide the real result.
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
