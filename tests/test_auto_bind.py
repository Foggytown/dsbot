"""Channels are bound from a file, not from an environment variable.

`DSBOT_AUTO_BIND_CHANNELS` was documented in `config.py` as an "escape hatch for panels
that provide no shell". Nothing ever read it - `auto_bind_pairs()` had no callers - so a
channel configured that way would simply never be bound, while the bot started happily and
reported `no_channel_bindings`. A promise the code cannot keep is worse than no promise, so
the setting is gone.

The panel-friendly route is the file in the data directory: the panel keeps only `/data`,
the bot seeds it once from `forms/channels.yaml`, and re-reads it while running.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from dsbot.config import Settings
from dsbot.storage import repos
from dsbot.storage.db import Database


def test_the_auto_bind_setting_is_gone() -> None:
    """It was advertised and never implemented; leaving it would invite configuration that
    silently does nothing."""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert not hasattr(settings, "auto_bind_channels")
    assert not hasattr(settings, "auto_bind_pairs")


def test_the_channels_file_is_derived_from_the_data_directory() -> None:
    """The file that *does* work, and why it needs no environment variable."""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    path = settings.resolved_channels_file()
    assert path.name == "channels.yaml"
    assert path == settings.state_dir() / "channels.yaml"


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(
        Settings(_env_file=None, database_url=f"sqlite+aiosqlite:///{tmp_path / 'ab.db'}")  # type: ignore[call-arg]
    )
    await database.create_all()
    yield database
    await database.dispose()


async def test_a_channel_is_bound_by_importing_its_form(db: Database) -> None:
    """The actual startup path: channels file -> import_forms -> visible binding."""
    from dsbot.modules.reports.authoring import import_forms
    from dsbot.modules.reports.yaml_form import parse_form_file

    schema = parse_form_file(Path("forms/training.yaml"))
    async with db.write_session() as session:
        await import_forms(
            session,
            channel_id=222,
            payload={"schemas": [schema.model_dump(mode="json")]},
            guild_id=111,
        )

    async with db.session() as session:
        binding = await repos.get_binding_by_channel(session, 222)
    assert binding is not None
    assert binding.guild_id == 111
