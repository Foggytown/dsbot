"""Shows exactly what a staff member can type, including every period option."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import discord

from dsbot.config import Settings
from dsbot.discord.commands import AdminCommands
from dsbot.discord.count_commands import CountCommands
from dsbot.discord.results_commands import ResultsCommands
from dsbot.storage.db import Database


def dump(group: object) -> None:
    for command in group.commands:  # type: ignore[attr-defined]
        print(f"/{group.name} {command.name}")  # type: ignore[attr-defined]
        print(f"  {command.description}")
        for param in command.parameters:
            required = "required" if param.required else "optional"
            choices = ""
            if param.choices:
                choices = "  " + ", ".join(c.value for c in param.choices)
            print(f"    {param.name:<12} {param.type.name:<8} {required}{choices}")
        print()


async def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    settings = Settings(database_url=f"sqlite+aiosqlite:///{(tmp / 'c.db').as_posix()}")
    db = Database(settings)
    try:
        await db.prepare()
        dump(CountCommands(db, None, "Europe/Moscow"))
        dump(ResultsCommands(db))
        dump(AdminCommands(db))
    finally:
        await db.dispose()
    _ = discord  # imported so the app_commands types resolve


asyncio.run(main())
