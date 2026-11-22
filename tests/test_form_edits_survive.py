"""An admin's form edit made in Discord must survive a restart.

Every start re-imports `forms/*.yaml` for each bound channel. Before the fingerprint
column, that import overwrote the stored definition unconditionally, so `/dsbot add-value
… MED` worked until the next deploy and then silently vanished. The stored definition is
meant to be the source of truth (AGENTS #3).

Telling the three cases apart needs a record of what the file said last time, because
"stored differs from file" is true both when the admin edited the form and when the file
was edited in git:

  file unchanged, stored changed  -> the admin edited it, keep it
  file changed                    -> git wins, apply the file
  neither changed                 -> nothing to do
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from copy import deepcopy
from pathlib import Path

import pytest
from sqlalchemy import select

from dsbot.config import Settings
from dsbot.modules.reports import authoring
from dsbot.modules.reports.schema import DEFAULT_SCHEMAS
from dsbot.modules.reports.yaml_form import parse_form_file
from dsbot.storage import admin
from dsbot.storage.db import Database
from dsbot.storage.models import FormSchema

CHANNEL = 910
GUILD = 911
KEY = "training_report"


def payload_from(path: Path = Path("forms/training.yaml")) -> dict:
    schema = parse_form_file(path)
    return json.loads(json.dumps({"schemas": [schema.to_definition()]}))


@pytest.fixture
async def bound(tmp_path: Path) -> AsyncIterator[Database]:
    db = Database(Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'forms.db'}"))
    await db.create_all()
    await db.prepare()
    schema = parse_form_file(Path("forms/training.yaml"))
    async with db.write_session() as session:
        await admin.ensure_schema_seed(session, DEFAULT_SCHEMAS)
        await admin.bind_channel(
            session,
            guild_id=GUILD,
            channel_id=CHANNEL,
            module_key="reports",
            schema_key=schema.key,
        )
    yield db
    await db.dispose()


async def options(db: Database) -> list[str]:
    async with db.session() as session:
        row = await session.scalar(select(FormSchema).where(FormSchema.key == KEY))
    assert row is not None
    for field in row.definition["fields"]:
        if field["id"] == "qualification":
            return list(field.get("options") or [])
    return []


async def version(db: Database) -> int:
    async with db.session() as session:
        row = await session.scalar(select(FormSchema).where(FormSchema.key == KEY))
    assert row is not None
    return row.version


async def first_import(db: Database) -> authoring.ImportResult:
    async with db.write_session() as session:
        return await authoring.import_forms(session, channel_id=CHANNEL, payload=payload_from())


async def test_an_admin_edit_survives_the_next_import(bound: Database) -> None:
    """The bug: /dsbot add-value worked until the restart, then vanished."""
    await first_import(bound)
    async with bound.write_session() as session:
        await authoring.edit_field_values(
            session, channel_id=CHANNEL, field_id="qualification", add=["MED"]
        )
    assert "MED" in await options(bound)

    # A restart: the same file is imported again.
    result = await first_import(bound)

    assert "MED" in await options(bound), "the admin's value must survive a restart"
    assert result.preserved == [KEY], "the import must report that it kept the form"
    assert result.updated == []


async def test_a_change_in_the_file_wins_over_the_admin_edit(bound: Database) -> None:
    """Decided explicitly: a commit is a deliberate act, so git is authoritative."""
    await first_import(bound)
    async with bound.write_session() as session:
        await authoring.edit_field_values(
            session, channel_id=CHANNEL, field_id="qualification", add=["MED"]
        )

    changed = payload_from()
    for field in changed["schemas"][0]["fields"]:
        if field["id"] == "qualification":
            field["options"] = [*list(field["options"]), "NEW"]

    async with bound.write_session() as session:
        result = await authoring.import_forms(session, channel_id=CHANNEL, payload=changed)

    current = await options(bound)
    assert "NEW" in current, "the file must be applied"
    assert "MED" not in current, "the admin edit is intentionally overwritten"
    assert result.updated == [KEY]
    assert result.preserved == []


async def test_an_unchanged_form_is_not_rewritten(bound: Database) -> None:
    """A redeploy with no changes must not bump the version or touch the row."""
    await first_import(bound)
    before = await version(bound)

    result = await first_import(bound)

    assert await version(bound) == before, "no version bump for an unchanged form"
    assert result.updated == []
    assert result.preserved == [], "nothing to preserve: the stored form still matches"


async def test_a_reformatting_of_the_yaml_is_not_a_change(bound: Database) -> None:
    """The fingerprint covers the compiled definition, not the file bytes.

    Otherwise editing a comment or reordering keys would look like a form change and
    stomp the admin's edit.
    """
    await first_import(bound)
    async with bound.write_session() as session:
        await authoring.edit_field_values(
            session, channel_id=CHANNEL, field_id="qualification", add=["MED"]
        )

    # Same meaning, different key order.
    reshuffled = payload_from()
    reshuffled["schemas"][0] = dict(reversed(list(reshuffled["schemas"][0].items())))

    async with bound.write_session() as session:
        result = await authoring.import_forms(session, channel_id=CHANNEL, payload=reshuffled)

    assert "MED" in await options(bound), "a cosmetic change must not discard the edit"
    assert result.preserved == [KEY]


async def test_the_fingerprint_matches_the_stored_definition(bound: Database) -> None:
    """Sanity: the stored hash describes the stored definition, not something stale."""
    await first_import(bound)
    async with bound.session() as session:
        row = await session.scalar(select(FormSchema).where(FormSchema.key == KEY))
    assert row is not None
    assert row.import_hash is not None
    assert row.import_hash == authoring._fingerprint(row.definition)

    # And after an admin edit the stored definition no longer matches its own fingerprint,
    # which is exactly the signal the import reads.
    async with bound.write_session() as session:
        await authoring.edit_field_values(
            session, channel_id=CHANNEL, field_id="qualification", add=["MED"]
        )
    async with bound.session() as session:
        row = await session.scalar(select(FormSchema).where(FormSchema.key == KEY))
    assert row is not None
    assert row.import_hash != authoring._fingerprint(deepcopy(row.definition))
