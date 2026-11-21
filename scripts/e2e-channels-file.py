"""Proves the channels file works from /data and can be edited at runtime.

uv run python scripts/e2e-channels-file.py
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

from dsbot.config import Settings
from dsbot.modules.reports.channels import (
    file_signature,
    load_channels_file,
    seed_channels_file,
)
from dsbot.modules.reports.channels_sync import sync_channels
from dsbot.storage import admin
from dsbot.storage.db import Database

GUILD = 900


async def main() -> None:
    data_dir = Path(tempfile.mkdtemp())  # stands in for /data
    checkout = Path(tempfile.mkdtemp())  # stands in for the uploaded code
    settings = Settings(
        data_dir=str(data_dir),
        database_url=f"sqlite+aiosqlite:///{(data_dir / 'dsbot.db').as_posix()}",
    )
    db = Database(settings)
    try:
        await db.prepare()

        # The shipped template, as it arrives in the checkout.
        template = checkout / "forms" / "channels.yaml"
        template.parent.mkdir(parents=True)
        template.write_text(
            f"channels:\n  - guild: {GUILD}\n    channel: 500\n    form: training_report\n",
            encoding="utf-8",
        )

        live = settings.resolved_channels_file()
        print(f"live file: {live}")
        assert live.parent == data_dir, "the live file must be on the persistent volume"

        print("\nFIRST START")
        print("  seeded:", seed_channels_file(live, template))
        async with db.write_session() as session:
            await sync_channels(session, load_channels_file(live))
        async with db.session() as session:
            print("  bound:", [b.channel_id for b in await admin.list_all_bindings(session)])

        # --- admin edits the file on the server: adds a channel ---
        print("\nADMIN ADDS A CHANNEL BY EDITING THE FILE")
        before = file_signature(live)
        live.write_text(
            f"channels:\n"
            f"  - guild: {GUILD}\n    channel: 500\n    form: training_report\n"
            f"  - guild: {GUILD}\n    channel: 501\n    form: training_report\n",
            encoding="utf-8",
        )
        after = file_signature(live)
        print(f"  change detected: {before != after}")

        async with db.write_session() as session:
            await sync_channels(session, load_channels_file(live))
        async with db.session() as session:
            bound = sorted(b.channel_id for b in await admin.list_all_bindings(session))
        print("  bound now:", bound)
        assert bound == [500, 501]

        # --- a redeploy: checkout replaced, /data kept ---
        print("\nREDEPLOY (checkout replaced, /data kept)")
        shutil.rmtree(checkout)
        checkout = Path(tempfile.mkdtemp())
        template2 = checkout / "forms" / "channels.yaml"
        template2.parent.mkdir(parents=True)
        template2.write_text(
            f"channels:\n  - guild: {GUILD}\n    channel: 500\n    form: training_report\n",
            encoding="utf-8",
        )
        reseeded = seed_channels_file(live, template2)
        print(f"  re-seeded from the new checkout: {reseeded} (must be False)")
        async with db.write_session() as session:
            await sync_channels(session, load_channels_file(live))
        async with db.session() as session:
            bound = sorted(b.channel_id for b in await admin.list_all_bindings(session))
        print("  still bound:", bound)
        assert reseeded is False, "a redeploy overwrote the admin's file"
        assert bound == [500, 501], "the admin's added channel was lost"

        print("\nOK: the file lives in /data, is editable at runtime, and survives deploys")
    finally:
        await db.dispose()
        shutil.rmtree(data_dir, ignore_errors=True)
        shutil.rmtree(checkout, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
