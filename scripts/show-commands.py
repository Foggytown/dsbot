"""Shows exactly what a staff member can type, including every period option."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import discord

from dsbot.config import Settings
from dsbot.discord.count_commands import CountCommands
from dsbot.storage.db import Database


async def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    settings = Settings(database_url=f"sqlite+aiosqlite:///{(tmp / 'c.db').as_posix()}")
    db = Database(settings)
    try:
        await db.prepare()
        group = CountCommands(db, None, "Europe/Moscow")
        for command in group.commands:
            print(f"/reports {command.name}")
            print(f"  {command.description}")
            for param in command.parameters:
                required = "required" if param.required else "optional"
                choices = ""
                if param.choices:
                    choices = "  " + ", ".join(c.value for c in param.choices)
                print(f"    {param.name:<8} {param.type.name:<6} {required}{choices}")
            print()
    finally:
        await db.dispose()
    _ = discord  # imported so the app_commands types resolve


asyncio.run(main())
