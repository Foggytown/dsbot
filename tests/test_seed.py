"""Seeding must never destroy a configured form.

This bug shipped: `dsbot bind` re-seeded the built-in definition and overwrote whatever
the admin had configured, silently wiping rules and allowed-value lists. The stored
definition is the source of truth.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import select

from dsbot.config import Settings
from dsbot.modules.reports.schema import FormSchemaDef
from dsbot.modules.reports.yaml_form import load_default_schemas, parse_form_file
from dsbot.storage import admin
from dsbot.storage.db import Database
from dsbot.storage.models import FormSchema


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'seed.db'}")
    database = Database(settings)
    await database.create_all()
    yield database
    await database.dispose()


def configured_form() -> FormSchemaDef:
    form = parse_form_file(Path("forms/training.yaml")).model_copy(deep=True)
    form.key = "training_report"
    # An admin change that must survive re-seeding.
    form.field_by_id("qualification").options = ["ONLY-THIS"]  # type: ignore[union-attr]
    return form


async def test_seed_does_not_overwrite_an_existing_form(db: Database) -> None:
    async with db.write_session() as session:
        await admin.ensure_schema_seed(session, [configured_form()])

    async with db.write_session() as session:
        # A second seed with the shipped defaults must not touch the stored row.
        await admin.ensure_schema_seed(session, load_default_schemas())

    async with db.session() as session:
        row = await session.scalar(select(FormSchema).where(FormSchema.key == "training_report"))
    assert row is not None
    assert row.definition["fields"][1]["options"] == ["ONLY-THIS"]


async def test_seed_inserts_a_missing_form(db: Database) -> None:
    async with db.write_session() as session:
        ids = await admin.ensure_schema_seed(session, load_default_schemas())
    assert "training_report" in ids


async def test_seed_overwrite_is_opt_in(db: Database) -> None:
    async with db.write_session() as session:
        await admin.ensure_schema_seed(session, [configured_form()])

    async with db.write_session() as session:
        await admin.ensure_schema_seed(session, load_default_schemas(), overwrite=True)

    async with db.session() as session:
        row = await session.scalar(select(FormSchema).where(FormSchema.key == "training_report"))
    assert row is not None
    # Overwritten on purpose, so back to the shipped options.
    assert "ONLY-THIS" not in row.definition["fields"][1]["options"]


async def test_seed_is_idempotent(db: Database) -> None:
    async with db.write_session() as session:
        await admin.ensure_schema_seed(session, load_default_schemas())
    async with db.write_session() as session:
        await admin.ensure_schema_seed(session, load_default_schemas())

    async with db.session() as session:
        rows = (await session.scalars(select(FormSchema))).all()
    assert len(rows) == 1


def test_default_schemas_come_from_the_yaml_file() -> None:
    """The shipped YAML is the default, not the legacy code copy."""
    schemas = load_default_schemas()
    assert schemas
    training = next(s for s in schemas if s.key == "training_report")
    # The YAML has rules; the legacy code copy had none, which is how this was noticed.
    assert training.rules
    ids = {f.id for f in training.fields}
    assert ids == {"identity", "qualification", "photos", "previous_report"}
