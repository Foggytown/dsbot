"""Auto-binding from configuration.

Panels often give no shell, so a fresh deployment must be able to bind its channels
from `.env`. Without it the bot starts, reports `no_channel_bindings`, and silently
ignores every message.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from dsbot.config import Settings
from dsbot.storage import repos
from dsbot.storage.db import Database


def settings(value: str) -> Settings:
    return Settings(_env_file=None, auto_bind_channels=value)  # type: ignore[call-arg]


def test_single_pair() -> None:
    assert settings("1209516013779943544:1573045809774067812").auto_bind_pairs() == [
        (1209516013779943544, 1573045809774067812)
    ]


def test_multiple_pairs() -> None:
    pairs = settings("111:222, 333:444").auto_bind_pairs()
    assert pairs == [(111, 222), (333, 444)]


def test_whitespace_and_newlines_are_tolerated() -> None:
    pairs = settings("111:222,\n  333:444 ,\n").auto_bind_pairs()
    assert pairs == [(111, 222), (333, 444)]


def test_empty_means_nothing() -> None:
    assert settings("").auto_bind_pairs() == []


def test_malformed_entries_are_skipped_not_fatal() -> None:
    """A typo in one pair must not stop the others from binding."""
    pairs = settings("111:222,not-a-pair,333:444").auto_bind_pairs()
    assert pairs == [(111, 222), (333, 444)]


def test_slash_separator_is_accepted() -> None:
    assert settings("111/222").auto_bind_pairs() == [(111, 222)]


def test_semicolons_also_separate() -> None:
    assert settings("111:222;333:444").auto_bind_pairs() == [(111, 222), (333, 444)]


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(
        Settings(_env_file=None, database_url=f"sqlite+aiosqlite:///{tmp_path / 'ab.db'}")  # type: ignore[call-arg]
    )
    await database.create_all()
    yield database
    await database.dispose()


async def test_binding_created_from_config(db: Database) -> None:
    """The actual startup path: config -> import_forms -> visible binding."""
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
