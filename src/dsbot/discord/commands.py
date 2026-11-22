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
from dsbot.storage import ranks_repo, repos
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

    # --- quotas and reporting format --------------------------------------

    @app_commands.command(
        name="hide-tier",
        description="Hide a tier from this channel's report (or bring it back)",
    )
    @app_commands.describe(
        tier="Tier label or key to hide or show, e.g. Instructor",
        mode="hide to stop showing it, show to bring it back",
    )
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="Скрыть", value="hide"),
            app_commands.Choice(name="Показать снова", value="show"),
        ]
    )
    async def hide_tier(
        self,
        interaction: discord.Interaction,
        tier: str,
        mode: app_commands.Choice[str] | None = None,
    ) -> None:
        """Declutter the report without changing who is counted."""
        if not await _require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        channel_id = interaction.channel_id
        assert channel_id is not None

        chosen = mode.value if mode is not None else "hide"
        try:
            detail = await set_hidden_tiers(
                self._db,
                channel_id,
                hide=[tier] if chosen == "hide" else [],
                show=[tier] if chosen == "show" else [],
            )
        except ValueError as exc:
            await interaction.followup.send(f"Rejected: {exc}", ephemeral=True)
            return
        await interaction.followup.send(
            f"{detail}\nНа подсчёт это не влияет — только на то, что видно в отчёте.",
            ephemeral=True,
        )

    @hide_tier.autocomplete("tier")
    async def _tier_label_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        channel_id = interaction.channel_id
        if channel_id is None:
            return []
        try:
            async with self._db.session() as session:
                binding = await repos.get_binding_by_channel(session, channel_id)
                if binding is None:
                    return []
                roster = []
                guild_id = binding.rank_source_guild_id or binding.guild_id
                ladders = await ranks_repo.load_all_ladders(session)
                group = ladders.get(guild_id)
                if group is not None and group.quota_ladder_id is not None:
                    roster = list(await repos.list_roster(session, group.quota_ladder_id))
        except Exception:
            return []
        labels: list[str] = []
        for row in roster:
            if row.tier_label and row.tier_label not in labels:
                labels.append(row.tier_label)
        needle = current.lower()
        return [
            app_commands.Choice(name=label, value=label)
            for label in labels
            if needle in label.lower()
        ][:MAX_CHOICES]

    @app_commands.command(
        name="roster-refresh",
        description="Обновить список людей из ролей (нужно перед подсчётом)",
    )
    async def roster_refresh(self, interaction: discord.Interaction) -> None:
        """Scrape who holds the quota roles, straight from the running bot.

        This exists because the hosting panel gives no shell: `dsbot roster-refresh` needs
        one, and without it a count reports "nobody found" forever. The bot is already
        connected to the gateway, so it can read members itself.
        """
        if not await _require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        guild_id, ladder_id, ladder = await self._quota_ladder(interaction.channel_id)
        if ladder is None:
            await interaction.followup.send(
                "В канале не настроена квота-лестница, считать некого.", ephemeral=True
            )
            return

        intents = getattr(interaction.client, "intents", None)
        if not getattr(intents, "members", False):
            await interaction.followup.send(
                "У бота выключен Members intent, поэтому Discord не отдаёт список "
                "участников. Включите SERVER MEMBERS INTENT в developer portal и "
                "DSBOT_ENABLE_MEMBERS_INTENT=true.",
                ephemeral=True,
            )
            return

        from dsbot.modules.ranks.roster import DiscordRosterSource, RosterService

        service = RosterService(self._db, DiscordRosterSource(interaction.client))
        result = await service.refresh(guild_id, ladder, ladder_id=ladder_id)

        if not result.ok:
            await interaction.followup.send(
                f"Не удалось обновить список: {result.error}", ephemeral=True
            )
            return
        await interaction.followup.send(
            f"Список обновлён: **{len(result.members)}** из {result.total_members} "
            f"участников подходят под роли квоты. Теперь `/reports count` знает, кого "
            f"считать.",
            ephemeral=True,
        )
        log.info(
            "roster_refreshed_from_discord",
            guild_id=guild_id,
            matched=len(result.members),
            scanned=result.total_members,
            user_id=interaction.user.id,
        )

    async def _quota_ladder(self, channel_id: int | None) -> tuple[int, int, Any]:
        """The channel's rank guild and quota ladder, for a roster scrape."""
        if channel_id is None:
            return 0, 0, None
        async with self._db.session() as session:
            binding = await repos.get_binding_by_channel(session, channel_id)
            if binding is None:
                return 0, 0, None
            guild_id = binding.rank_source_guild_id or binding.guild_id
            ladders = await ranks_repo.load_all_ladders(session)
        group = ladders.get(guild_id)
        if group is None or group.quota_ladder_id is None:
            return guild_id, 0, None
        return guild_id, group.quota_ladder_id, group

    @app_commands.command(
        name="quota-set",
        description="Override a tier's weekly requirement for this channel",
    )
    @app_commands.describe(
        tier="Tier key from the quota ladder, e.g. instructor",
        count="Reports required per week for THIS channel (0 removes the requirement)",
    )
    async def quota_set(self, interaction: discord.Interaction, tier: str, count: int) -> None:
        """Per-channel override. The normal number lives in the quota ladder file."""
        if not await _require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        channel_id = interaction.channel_id
        assert channel_id is not None

        try:
            detail = await set_quota_for_channel(self._db, channel_id, tier, count)
        except ValueError as exc:
            await interaction.followup.send(f"Rejected: {exc}", ephemeral=True)
            return
        await interaction.followup.send(detail, ephemeral=True)
        log.info("admin_quota_set", channel_id=channel_id, tier=tier, count=count)

    @app_commands.command(
        name="report-target",
        description="Choose where the weekly report is also published",
    )
    @app_commands.describe(
        channel="Channel to publish into, or leave empty to only answer privately",
    )
    async def report_target(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ) -> None:
        """Where the periodic report goes. Empty channel means ephemeral only."""
        if not await _require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        channel_id = interaction.channel_id
        assert channel_id is not None

        # The target is usually a *different* channel from the report one, because the
        # table belongs somewhere quieter. That channel is not bound to a form, and
        # requiring it to be made the setting unusable in exactly the case it exists for:
        # the call raised ValueError and the interaction failed with no message.
        source_channel = channel_id
        async with self._db.session() as session:
            binding = await repos.get_binding_by_channel(session, channel_id)
            if binding is None and channel is not None:
                binding = await repos.get_binding_by_channel(session, channel.id)
                if binding is not None:
                    source_channel = channel.id
        if binding is None:
            await interaction.followup.send(
                "Этот канал не привязан к форме рапорта, и публиковать нечего. "
                "Вызовите команду в канале рапортов, указав канал для публикации.",
                ephemeral=True,
            )
            return

        try:
            await set_report_target(
                self._db,
                source_channel,
                publish_to=channel.id if channel is not None else 0,
            )
        except ValueError as exc:
            await interaction.followup.send(f"Не получилось: {exc}", ephemeral=True)
            return

        if channel is None:
            await interaction.followup.send(
                "Отчёты больше не публикуются в канал — только в ответ на команду.",
                ephemeral=True,
            )
            return

        # Verify the bot can actually post BEFORE saving. A destination that is stored but
        # forbidden is the worst outcome: the setting looks successful, every later count
        # fails, and the failure only shows up in the log. That is exactly what happened
        # with a 403 "Missing Access" on this server.
        problem = await can_post_to(interaction.client, channel)
        if problem:
            await interaction.followup.send(
                f"⚠️ Канал {channel.mention} выбран, но публиковать туда не получится: "
                f"{problem}\n\n"
                "Настройка **не сохранена**. Дайте боту доступ к этому каналу "
                "(«Просматривать канал» и «Отправлять сообщения») и повторите команду.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"Отчёты будут публиковаться в {channel.mention}. Проверено: бот может туда писать.",
            ephemeral=True,
        )
        log.info("admin_report_target", channel_id=source_channel, publish_to=channel.id)

    @app_commands.command(
        name="ladder-pin",
        description="Pin this channel to one ladder in its role server",
    )
    @app_commands.describe(key="Ladder key, or empty to use any ladder in that guild")
    async def ladder_pin(self, interaction: discord.Interaction, key: str | None = None) -> None:
        """Channel-level hierarchy, set from Discord instead of a file."""
        if not await _require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        channel_id = interaction.channel_id
        assert channel_id is not None

        try:
            detail = await pin_channel_ladder(self._db, channel_id, key)
        except ValueError as exc:
            await interaction.followup.send(f"Rejected: {exc}", ephemeral=True)
            return
        await interaction.followup.send(detail, ephemeral=True)
        log.info("admin_ladder_pinned", channel_id=channel_id, key=key)

    @ladder_pin.autocomplete("key")
    @quota_set.autocomplete("tier")
    async def _ladder_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        channel_id = interaction.channel_id
        if channel_id is None:
            return []
        try:
            async with self._db.session() as session:
                binding = await repos.get_binding_by_channel(session, channel_id)
                if binding is None:
                    return []
                guild_id = binding.rank_source_guild_id or binding.guild_id
                ladders = await ranks_repo.load_all_ladders(session)
        except Exception:
            return []
        group = ladders.get(guild_id)
        if group is None:
            return []
        needle = current.lower()
        names: list[str] = []
        for index in group.indexes:
            if needle in index.key.lower():
                names.append(index.key)
            for tier_key in _tier_keys(index):
                if needle in tier_key.lower() and tier_key not in names:
                    names.append(tier_key)
        return [app_commands.Choice(name=n, value=n) for n in names][:MAX_CHOICES]


def _tier_keys(index: Any) -> list[str]:
    """Tier keys of one ladder. Tiers are not indexed by key, so they are derived."""
    keys: list[str] = []
    for entry in index.by_role.values():
        if entry[0] not in keys:
            keys.append(entry[0])
    return keys


async def set_hidden_tiers(
    db: Database, channel_id: int, *, hide: list[str], show: list[str]
) -> str:
    """Hide or show tiers in this channel's report.

    Stored per channel because "too cluttered" is a per-channel judgement: one channel may
    want only the instructors, another everything. Matching is case-insensitive so a typo
    in capitalisation does not silently do nothing.
    """
    async with db.write_session() as session:
        binding = await repos.get_binding_by_channel(session, channel_id)
        if binding is None:
            raise ValueError("this channel is not bound to a report form")
        config = dict(binding.config or {})
        current = [str(value) for value in (config.get("hidden_tiers") or [])]

        lowered_hide = {h.strip().lower() for h in hide if h.strip()}
        lowered_show = {s.strip().lower() for s in show if s.strip()}
        kept = [t for t in current if t.strip().lower() not in lowered_show]
        for name in hide:
            cleaned = name.strip()
            if cleaned and cleaned.lower() not in {k.lower() for k in kept}:
                kept.append(cleaned)

        config["hidden_tiers"] = kept
        binding.config = config
        _ = lowered_hide

    if not kept:
        return "Все тиры снова показываются."
    return "Скрыто из отчёта: " + ", ".join(kept)


def _split(raw: str) -> list[str]:
    import re

    return [v for v in re.split(r"[,\s]+", raw) if v]


async def set_quota_for_channel(db: Database, channel_id: int, tier: str, count: int) -> str:
    """Set one tier's weekly requirement for a channel. Raises ValueError to refuse."""
    from sqlalchemy import select

    from dsbot.storage import admin, ranks_repo
    from dsbot.storage.models import RankTier

    async with db.session() as session:
        binding = await repos.get_binding_by_channel(session, channel_id)
        if binding is None:
            raise ValueError("this channel is not bound to a report form")
        guild_id = binding.rank_source_guild_id or binding.guild_id
        ladders = await ranks_repo.load_all_ladders(session)
        group = ladders.get(guild_id)
        if group is None or group.quota_ladder_id is None:
            raise ValueError(
                "no quota ladder is configured for this channel's role server. "
                "Ask an operator to apply a ladder with purpose: quota."
            )
        ladder_id = group.quota_ladder_id
        tiers = (
            await session.scalars(select(RankTier).where(RankTier.ladder_id == ladder_id))
        ).all()

    match = next((row for row in tiers if row.key == tier), None)
    if match is None:
        known = ", ".join(sorted(row.key for row in tiers)) or "(none)"
        raise ValueError(f"unknown tier {tier!r}. Known: {known}")

    async with db.write_session() as session:
        await admin.set_quota(
            session,
            ladder_id=ladder_id,
            tier_id=match.id,
            binding_id=binding.id,
            required_count=count,
        )
    label = match.label or match.key
    if count <= 0:
        return f"{label}: требование снято для этого канала (0 в неделю)."
    note = ""
    if match.required_per_week not in (None, count):
        note = f" (в лестнице указано {match.required_per_week}; там не меняется)"
    return f"{label}: {count} рапорт(ов) в неделю для этого канала{note}."


async def set_report_target(
    db: Database,
    channel_id: int,
    *,
    publish_to: int,
) -> None:
    """Store where the report is published.

    Persisted in the binding row, which lives in the database on the panel's /data
    volume, so a setting made from Discord survives a redeploy.
    """
    async with db.write_session() as session:
        binding = await repos.get_binding_by_channel(session, channel_id)
        if binding is None:
            raise ValueError("this channel is not bound to a report form")
        config = dict(binding.config or {})
        config["report_channel_id"] = None if publish_to == 0 else publish_to
        config.pop("report_title", None)
        binding.config = config


async def can_post_to(client: Any, channel: Any) -> str:
    """Try to post a throwaway message. Returns the problem, or "" when it works.

    Probed by doing, not by computing permissions. `permissions_for` needs the Members
    intent and gets channel overwrites subtly wrong; a real send is the only answer that
    cannot be wrong. The probe deletes itself, so the channel is left as it was.

    This exists because a 403 "Missing Access" was only visible in the log: the setting
    saved fine, then every published report failed silently while the count looked correct.
    """
    try:
        probe = await channel.send(
            "dsbot: проверка прав публикации (удалится само)",
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.Forbidden:
        return "у бота нет прав «Просматривать канал» и «Отправлять сообщения» в этом канале"
    except discord.HTTPException as exc:
        return f"Discord отклонил пробное сообщение: {exc.text or exc}"

    try:
        await probe.delete()
    except Exception:
        # Leaving one probe message behind is harmless; the permission was proven.
        log.warning("report_target_probe_not_deleted", channel_id=channel.id)
    return ""


async def pin_channel_ladder(db: Database, channel_id: int, key: str | None) -> str:
    """Pin or unpin this channel's hierarchy to one ladder."""
    from sqlalchemy import select

    from dsbot.storage.models import RankLadder

    async with db.session() as session:
        binding = await repos.get_binding_by_channel(session, channel_id)
        if binding is None:
            raise ValueError("this channel is not bound to a report form")
        guild_id = binding.rank_source_guild_id or binding.guild_id
        candidates = (
            await session.scalars(select(RankLadder).where(RankLadder.guild_id == guild_id))
        ).all()
        target = None
        wanted = (key or "").strip()
        if wanted:
            target = next((row for row in candidates if row.key == wanted), None)
            if target is None:
                known = ", ".join(sorted(row.key for row in candidates)) or "(none)"
                raise ValueError(f"no ladder {wanted!r} in that guild. Known: {known}")

    async with db.write_session() as session:
        binding = await repos.get_binding_by_channel(session, channel_id)
        if binding is None:
            raise ValueError("this channel is not bound to a report form")
        binding.rank_ladder_id = target.id if target is not None else None

    if target is None:
        return "Иерархия не закреплена: действуют все лестницы этого сервера."
    return f"Иерархия закреплена за лестницей «{target.key}»."


async def register_commands(bot: Any, db: Database) -> None:
    """Attach the command tree. Safe to call once at startup."""
    bot.tree.add_command(AdminCommands(db))
    log.info("slash_commands_registered", count=len(bot.tree.get_commands()))


__all__ = [
    "AdminCommands",
    "pin_channel_ladder",
    "register_commands",
    "set_hidden_tiers",
    "set_quota_for_channel",
    "set_report_target",
]
