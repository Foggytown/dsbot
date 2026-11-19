"""Form authoring: export/import a channel's schema as JSON.

The admin workflow this enables, today from the CLI and later from a slash command:

    1. dsbot form-export --channel <id>          -> forms/ch-<id>.json
    2. edit the JSON (add fields, change labels, required-ness, options)
    3. dsbot form-import --channel <id> --file forms/ch-<id>.json
    4. dsbot parse --file sample.txt             -> verify

Nothing here is channel-specific. A second channel with a different form is a second
JSON file, not code.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dsbot.modules.reports.schema import FormSchemaDef, from_definition
from dsbot.storage import admin
from dsbot.storage.models import BindingSchema, ChannelBinding, FormSchema

# A minimal starting point for a new channel form: two fields, no assumptions.
STARTER_DEFINITION: dict[str, Any] = {
    "key": "new_channel_form",
    "title": "",
    "locale": "ru",
    "template_markers": [],
    "anchors": [],
    "attribution_field": None,
    "attribution_requires_review": False,
    "min_score": 1.0,
    "fields": [
        {
            "id": "field_1",
            "labels": [],
            "path": "1",
            "type": "text",
            "required": True,
            "required_when": None,
            "options": [],
            "accepts_attachments": False,
            "hint": {},
        },
        {
            "id": "field_2",
            "labels": [],
            "path": "2",
            "type": "text",
            "required": True,
            "required_when": None,
            "options": [],
            "accepts_attachments": False,
            "hint": {},
        },
    ],
}


@dataclass(slots=True)
class FormBundle:
    """A channel's forms in editor-friendly form."""

    channel_id: int
    binding_id: int | None
    schemas: list[dict[str, Any]]

    def to_json(self) -> str:
        return json.dumps(
            {
                "channel_id": self.channel_id,
                "binding_id": self.binding_id,
                "schemas": self.schemas,
            },
            ensure_ascii=False,
            indent=2,
        )


async def export_forms(session: AsyncSession, channel_id: int) -> FormBundle:
    binding = await session.scalar(
        select(ChannelBinding).where(ChannelBinding.channel_id == channel_id)
    )
    if binding is None:
        return FormBundle(channel_id=channel_id, binding_id=None, schemas=[])

    rows = (
        await session.scalars(
            select(FormSchema)
            .join(BindingSchema, BindingSchema.schema_id == FormSchema.id)
            .where(BindingSchema.binding_id == binding.id)
            .order_by(BindingSchema.priority)
        )
    ).all()

    return FormBundle(
        channel_id=channel_id,
        binding_id=binding.id,
        schemas=[row.definition for row in rows],
    )


@dataclass(slots=True)
class ImportResult:
    imported: list[str]
    created: list[str]
    updated: list[str]


async def import_forms(
    session: AsyncSession,
    *,
    channel_id: int,
    payload: dict[str, Any],
    guild_id: int | None = None,
    rank_source_guild_id: int | None = None,
    locale: str = "ru",
    module_key: str = "reports",
) -> ImportResult:
    """Validate and store one or more schemas, then bind them to the channel.

    Validation is strict: an invalid definition is rejected *before* anything is
    written, so a bad edit cannot leave a channel half-configured.
    """
    raw_schemas = payload.get("schemas")
    if raw_schemas is None:
        # Allow a single bare schema document as well as the bundle format.
        raw_schemas = [payload]
    if not isinstance(raw_schemas, list) or not raw_schemas:
        raise ValueError("payload must contain a non-empty 'schemas' list")

    # Validate everything first. Pydantic raises here, before any write.
    definitions = [FormSchemaDef.model_validate(item) for item in raw_schemas]
    if len({d.key for d in definitions}) != len(definitions):
        raise ValueError("schema keys must be unique within a channel")

    binding = await session.scalar(
        select(ChannelBinding).where(ChannelBinding.channel_id == channel_id)
    )
    if binding is None:
        if guild_id is None:
            raise ValueError("channel is unbound; pass --guild to create the binding")
        # The channel row must exist before the binding's FK can point at it.
        await admin.ensure_guild_and_channel(session, guild_id=guild_id, channel_id=channel_id)
        binding = ChannelBinding(
            channel_id=channel_id,
            guild_id=guild_id,
            module_key=module_key,
            enabled=True,
            locale=locale,
            rank_source_guild_id=rank_source_guild_id,
            config={},
        )
        session.add(binding)
        await session.flush()
    else:
        binding.enabled = True
        binding.locale = locale
        if rank_source_guild_id is not None:
            binding.rank_source_guild_id = rank_source_guild_id

    created: list[str] = []
    updated: list[str] = []

    for priority, definition in enumerate(definitions, start=1):
        row = await session.scalar(select(FormSchema).where(FormSchema.key == definition.key))
        if row is None:
            row = FormSchema(
                key=definition.key,
                title=definition.title,
                locale=definition.locale,
                definition=definition.to_definition(),
            )
            session.add(row)
            await session.flush()
            created.append(definition.key)
        else:
            row.title = definition.title
            row.locale = definition.locale
            row.definition = definition.to_definition()
            row.version += 1
            updated.append(definition.key)

        link = await session.scalar(
            select(BindingSchema).where(
                BindingSchema.binding_id == binding.id,
                BindingSchema.schema_id == row.id,
            )
        )
        if link is None:
            session.add(BindingSchema(binding_id=binding.id, schema_id=row.id, priority=priority))
        else:
            link.priority = priority

    return ImportResult(imported=[d.key for d in definitions], created=created, updated=updated)


def load_definition_file(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("form file must contain a JSON object")
    return data


def validate_definition(payload: dict[str, Any]) -> list[FormSchemaDef]:
    """Parse without writing. Used by `form-check` and by import pre-flight."""
    raw = payload.get("schemas")
    items = raw if isinstance(raw, list) else [payload]
    return [from_definition(item) for item in items]
