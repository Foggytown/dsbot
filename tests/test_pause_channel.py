"""Tests for pausing and resuming a channel.

Pausing is the supported way to take a form off a channel: the binding, its form, and
all stored history are kept, so resuming is one command and nothing is lost.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from dsbot.config import Settings
from dsbot.modules.reports.yaml_form import parse_form_file
from dsbot.storage import admin, repos
from dsbot.storage.db import Database

FORM = parse_form_file(Path("forms/training.yaml"))


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'pause.db'}")
    database = Database(settings)
    await database.create_all()
    yield database
    await database.dispose()


async def test_disabled_channel_is_not_treated_as_bound(db: Database) -> None:
    """The whole point: a paused channel stops being served."""
    from dsbot.modules.reports.authoring import import_forms

    async with db.write_session() as session:
        await import_forms(
            session,
            channel_id=500,
            payload={"schemas": [FORM.model_dump(mode="json")]},
            guild_id=900,
        )

    async with db.session() as session:
        assert await repos.get_binding_by_channel(session, 500) is not None

    async with db.write_session() as session:
        assert await admin.set_binding_enabled(session, 500, False) is True

    async with db.session() as session:
        # Disabled, so the live lookup returns nothing.
        assert await repos.get_binding_by_channel(session, 500) is None
        # But management can still see it.
        assert await repos.get_any_binding(session, 500) is not None


async def test_enable_restores_the_binding(db: Database) -> None:
    from dsbot.modules.reports.authoring import import_forms

    async with db.write_session() as session:
        await import_forms(
            session,
            channel_id=500,
            payload={"schemas": [FORM.model_dump(mode="json")]},
            guild_id=900,
        )
        await admin.set_binding_enabled(session, 500, False)

    async with db.write_session() as session:
        assert await admin.set_binding_enabled(session, 500, True) is True

    async with db.session() as session:
        binding = await repos.get_binding_by_channel(session, 500)
        assert binding is not None
        schemas = await repos.get_schemas_for_binding(session, binding.id)
        assert [s.key for s in schemas] == ["training_report"]


async def test_disable_keeps_the_form(db: Database) -> None:
    """Pausing must not destroy configuration."""
    from dsbot.modules.reports.authoring import import_forms

    async with db.write_session() as session:
        await import_forms(
            session,
            channel_id=500,
            payload={"schemas": [FORM.model_dump(mode="json")]},
            guild_id=900,
        )
        await admin.set_binding_enabled(session, 500, False)

    async with db.session() as session:
        binding = await repos.get_any_binding(session, 500)
        assert binding is not None
        schemas = await repos.get_schemas_for_binding(session, binding.id)
    assert [s.key for s in schemas] == ["training_report"]


async def test_disable_unbound_channel_reports_false(db: Database) -> None:
    async with db.write_session() as session:
        assert await admin.set_binding_enabled(session, 123456, False) is False


async def test_other_channels_are_unaffected(db: Database) -> None:
    from dsbot.modules.reports.authoring import import_forms

    payload = {"schemas": [FORM.model_dump(mode="json")]}
    async with db.write_session() as session:
        await import_forms(session, channel_id=500, payload=payload, guild_id=900)
        await import_forms(session, channel_id=501, payload=payload, guild_id=900)
        await admin.set_binding_enabled(session, 500, False)

    async with db.session() as session:
        assert await repos.get_binding_by_channel(session, 500) is None
        assert await repos.get_binding_by_channel(session, 501) is not None


async def test_applying_a_form_does_not_resume_a_paused_channel(db: Database) -> None:
    """Applying a form to a paused channel must not silently re-enable it."""
    from dsbot.modules.reports.authoring import import_forms

    payload = {"schemas": [FORM.model_dump(mode="json")]}
    async with db.write_session() as session:
        await import_forms(session, channel_id=500, payload=payload, guild_id=900)
        await admin.set_binding_enabled(session, 500, False)

    async with db.write_session() as session:
        await import_forms(session, channel_id=500, payload=payload, guild_id=900)

    async with db.session() as session:
        binding = await repos.get_any_binding(session, 500)
        assert binding is not None
        assert binding.enabled is False


async def test_list_all_bindings_includes_paused(db: Database) -> None:
    from dsbot.modules.reports.authoring import import_forms

    payload = {"schemas": [FORM.model_dump(mode="json")]}
    async with db.write_session() as session:
        await import_forms(session, channel_id=500, payload=payload, guild_id=900)
        await admin.set_binding_enabled(session, 500, False)

    async with db.session() as session:
        all_bindings = await admin.list_all_bindings(session)
        enabled = await repos.list_enabled_bindings(session)
    assert len(all_bindings) == 1
    assert len(enabled) == 0
