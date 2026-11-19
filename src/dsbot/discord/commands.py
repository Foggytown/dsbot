"""Admin slash commands.

Design goal: an admin configures the bot entirely from Discord. No restarts, no editing
files, no touching the database. Commands edit the stored form definition and the change
takes effect on the next message, because the parser is compiled from that definition.

Capability checks use the database `tier_capabilities` model later; for now the guard is
"you must have Manage Server", which is a Discord permission admins already have.
"""

from __future__ import annotations

from typing import Any

import discord
import structlog
from discord import app_commands

from dsbot.modules.reports.authoring import edit_field_values, export_forms
from dsbot.modules.reports.schema import FormSchemaDef
from dsbot.storage.db import Database

log = structlog.get_logger(__name__)

MAX_CHOICES = 25  # Discord's limit for autocomplete results


def _is_admin(interaction: discord.Interaction) -> bool:
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and (perms.manage_guild or perms.administrator))


async def _require_admin(interaction: discord.Interaction) -> bool:
    if _is_admin(interaction):
        return True
    await interaction.response.send_message(
        "Only server admins (Manage Server) can change bot configuration.",
        ephemeral=True,
    )
    return False


class AdminCommands(app_commands.Group, name="dsbot", description="dsbot configuration"):
    """Everything an admin needs, from inside Discord."""

    def __init__(self, db: Database) -> None:
        super().__init__()
        self._db = db

    # --- viewing -----------------------------------------------------------

    @app_commands.command(name="show", description="Show this channel's report form")
    async def show(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        channel_id = interaction.channel_id
        assert channel_id is not None

        async with self._db.session() as session:
            bundle = await export_forms(session, channel_id)

        if not bundle.schemas:
            await interaction.followup.send(
                "This channel has no report form bound.", ephemeral=True
            )
            return

        lines: list[str] = []
        for raw in bundle.schemas:
            schema = FormSchemaDef.model_validate(raw)
            lines.append(f"**{schema.title or schema.key}** (`{schema.key}`)")
            for spec in schema.fields:
                values = ""
                if spec.type == "enum":
                    values = f" → {', '.join(spec.options)}"
                elif spec.type == "formation_idn":
                    values = f" → {', '.join(spec.formations)}"
                required = "required" if spec.required else "optional"
                lines.append(
                    f"`{spec.path or '-'}` **{spec.id}** ({spec.type}, {required}){values}"
                )
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    # --- editing value lists ----------------------------------------------

    @app_commands.command(
        name="set-values",
        description="Replace the allowed values for a field, comma separated",
    )
    @app_commands.describe(
        field="Field id, e.g. qualification or formation_idn",
        values="Comma-separated values, e.g. INF, MED, ENG",
    )
    async def set_values(self, interaction: discord.Interaction, field: str, values: str) -> None:
        await self._edit_values(interaction, field, replace=_split(values))

    @app_commands.command(name="add-value", description="Allow one more value for a field")
    @app_commands.describe(field="Field id", value="A single value, e.g. MED")
    async def add_value(self, interaction: discord.Interaction, field: str, value: str) -> None:
        await self._edit_values(interaction, field, add=[value])

    @app_commands.command(name="remove-value", description="Disallow a value for a field")
    @app_commands.describe(field="Field id", value="A single value to remove")
    async def remove_value(self, interaction: discord.Interaction, field: str, value: str) -> None:
        await self._edit_values(interaction, field, remove=[value])

    @app_commands.command(name="set-required", description="Mark a field as required or optional")
    @app_commands.describe(field="Field id", required="True to require it")
    async def set_required(
        self, interaction: discord.Interaction, field: str, required: bool
    ) -> None:
        await self._edit_values(interaction, field, required=required)

    async def _edit_values(
        self,
        interaction: discord.Interaction,
        field: str,
        *,
        add: list[str] | None = None,
        remove: list[str] | None = None,
        replace: list[str] | None = None,
        required: bool | None = None,
    ) -> None:
        if not await _require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        channel_id = interaction.channel_id
        assert channel_id is not None

        try:
            async with self._db.write_session() as session:
                definition = await edit_field_values(
                    session,
                    channel_id=channel_id,
                    field_id=field,
                    add=add or [],
                    remove=remove or [],
                    replace=replace,
                    required=required,
                )
        except ValueError as exc:
            await interaction.followup.send(f"Rejected: {exc}", ephemeral=True)
            return

        schema = FormSchemaDef.model_validate(definition)
        spec = schema.field_by_id(field)
        detail = ""
        if spec is not None:
            if spec.type == "enum":
                detail = f"\noptions: {', '.join(spec.options)}"
            elif spec.type == "formation_idn":
                detail = f"\nformations: {', '.join(spec.formations)}"
            detail += f"\nrequired: {spec.required}"
        await interaction.followup.send(
            f"Updated `{field}`. In effect immediately.{detail}", ephemeral=True
        )
        log.info("admin_field_edited", channel_id=channel_id, field=field)

    # --- autocomplete ------------------------------------------------------

    @set_values.autocomplete("field")
    @add_value.autocomplete("field")
    @remove_value.autocomplete("field")
    @set_required.autocomplete("field")
    async def _field_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        channel_id = interaction.channel_id
        if channel_id is None:
            return []
        try:
            async with self._db.session() as session:
                bundle = await export_forms(session, channel_id)
        except Exception:
            return []
        names: list[str] = []
        for raw in bundle.schemas:
            schema = FormSchemaDef.model_validate(raw)
            for spec in schema.fields:
                names.append(spec.id)
        needle = current.lower()
        return [app_commands.Choice(name=n, value=n) for n in names if needle in n.lower()][
            :MAX_CHOICES
        ]

    @add_value.autocomplete("value")
    @remove_value.autocomplete("value")
    async def _value_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Suggest existing values so admins do not have to remember spellings."""
        channel_id = interaction.channel_id
        if channel_id is None:
            return []
        try:
            async with self._db.session() as session:
                bundle = await export_forms(session, channel_id)
        except Exception:
            return []
        values: list[str] = []
        for raw in bundle.schemas:
            schema = FormSchemaDef.model_validate(raw)
            for spec in schema.fields:
                values.extend(spec.options)
                values.extend(spec.formations)
        needle = current.lower()
        unique = sorted({v for v in values if needle in v.lower()})
        return [app_commands.Choice(name=v, value=v) for v in unique][:MAX_CHOICES]


def _split(raw: str) -> list[str]:
    import re

    return [v for v in re.split(r"[,\s]+", raw) if v]


async def register_commands(bot: Any, db: Database) -> None:
    """Attach the command tree. Safe to call once at startup."""
    bot.tree.add_command(AdminCommands(db))
    log.info("slash_commands_registered", count=len(bot.tree.get_commands()))


__all__ = ["AdminCommands", "register_commands"]
