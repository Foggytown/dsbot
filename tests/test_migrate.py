"""Upgrades to an existing database.

The server database is the only copy of months of reports, so the upgrade path matters
more than the fresh-install path. These tests build a database that looks like an
older release, then assert the upgrade adds what is missing and changes nothing else.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import text

from dsbot.config import Settings
from dsbot.storage.db import Database
from dsbot.storage.migrate import ADDITIONS, missing_tables, upgrade_schema


def open_db(tmp_path: Path) -> Database:
    return Database(Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'old.db'}"))


async def test_upgrade_adds_missing_columns_to_an_old_table(tmp_path: Path) -> None:
    db = open_db(tmp_path)
    try:
        # A table as an older release created it: no tier_key, no can_review.
        async with db.engine.begin() as conn:
            await conn.execute(
                text(
                    "CREATE TABLE message_reactions ("
                    "id INTEGER PRIMARY KEY, message_id BIGINT, channel_id BIGINT, "
                    "guild_id BIGINT, emoji_key TEXT, emoji_id BIGINT, emoji_name TEXT, "
                    "user_id BIGINT, tier_ordinal INTEGER, tier_label TEXT, added_at TEXT)"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO message_reactions "
                    "(id, message_id, channel_id, emoji_key, user_id) "
                    "VALUES (1, 55, 9, 'unicode:x', 7)"
                )
            )

        applied = await upgrade_schema(db)
        assert "message_reactions.tier_key" in applied
        assert "message_reactions.can_review" in applied

        async with db.engine.begin() as conn:
            columns = {
                row[1] for row in (await conn.execute(text("PRAGMA table_info(message_reactions)")))
            }
            rows = (await conn.execute(text("SELECT user_id FROM message_reactions"))).all()

        assert {"tier_key", "can_review"} <= columns
        # The old row survives: an upgrade must never rewrite history.
        assert rows == [(7,)]
    finally:
        await db.dispose()


async def test_upgrade_is_idempotent(tmp_path: Path) -> None:
    db = open_db(tmp_path)
    try:
        await db.create_all()
        await upgrade_schema(db)
        second = await upgrade_schema(db)
        assert second == []
    finally:
        await db.dispose()


async def test_every_addition_names_a_real_model_column() -> None:
    """A typo in the upgrade list would silently add a column nothing uses."""
    from dsbot.storage.models import Base

    for addition in ADDITIONS:
        table = Base.metadata.tables.get(addition.table)
        assert table is not None, f"unknown table {addition.table}"
        assert addition.column in table.columns, (
            f"{addition.table}.{addition.column} is not a model column"
        )


async def test_prepare_reports_nothing_missing_on_a_fresh_database(tmp_path: Path) -> None:
    db = open_db(tmp_path)
    try:
        await db.prepare()
        assert await missing_tables(db) == []
    finally:
        await db.dispose()
