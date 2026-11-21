"""Verifies the panel startup path now behaves correctly.

    uv run python scripts/e2e-panel-start.py

Simulates the two conditions that broke the first deployment and asserts the bot
recovers instead of silently serving nothing.
"""

from __future__ import annotations

import asyncio
import pathlib
import tempfile

from dsbot.config import Settings
from dsbot.modules.reports.channels import load_channels_file, seed_channels_file
from dsbot.modules.reports.channels_sync import sync_channels
from dsbot.storage import admin
from dsbot.storage.db import Database


async def main() -> None:
    data_dir = pathlib.Path(tempfile.mkdtemp())
    checkout = pathlib.Path(tempfile.mkdtemp())

    # A checkout with the shipped template, as the panel receives it.
    template = checkout / "forms" / "channels.yaml"
    template.parent.mkdir(parents=True)
    template.write_text(
        "channels:\n  - guild: 900\n    channel: 500\n    form: training_report\n",
        encoding="utf-8",
    )

    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        data_dir=str(data_dir),
        database_url=f"sqlite+aiosqlite:///{(data_dir / 'dsbot.db').as_posix()}",
    )

    print("1. channels file path")
    path = settings.resolved_channels_file()
    print(f"   resolved: {path}")
    assert path.parent == data_dir, "the file must live in the data directory"

    print("\n2. seeding from the checkout happens once")
    assert seed_channels_file(path, template) is True
    spec = load_channels_file(path)
    print(f"   seeded {len(spec.channels)} channel(s)")

    print("\n3. channels are bound without any setting being present")
    assert settings.channels_file == ""
    db = Database(settings)
    try:
        await db.prepare()
        async with db.write_session() as session:
            await sync_channels(session, spec)
        async with db.session() as session:
            bound = await admin.list_all_bindings(session)
        print(f"   bound: {[b.channel_id for b in bound]}")
        assert [b.channel_id for b in bound] == [500]
    finally:
        await db.dispose()

    print("\n4. a database outside the data dir is ignored, not obeyed")
    bad = Settings(
        _env_file=None,  # type: ignore[call-arg]
        data_dir=str(data_dir),
        database_url="sqlite+aiosqlite:///./var/dsbot.db",
    )
    problem = bad.database_location_problem()
    assert problem is not None
    assert bad.ignored_database_url() is not None, "the bad URL must be recorded as ignored"
    resolved = bad.resolved_database_url.replace("\\", "/")
    expected = str(data_dir).replace("\\", "/")
    assert expected in resolved
    print(f"   ignored: {bad.database_url}")
    print(f"   using  : {bad.resolved_database_url}")

    print("\n5. the correct config is accepted")
    assert settings.database_location_problem() is None
    print(f"   database: {settings.resolved_database_url}")

    print(
        "\nOK: channels bind on a fresh panel, and a stale database URL is "
        "ignored instead of losing data"
    )


if __name__ == "__main__":
    asyncio.run(main())
