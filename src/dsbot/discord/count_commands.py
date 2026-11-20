"""The staff-facing slash command: count reports on demand.

Design constraints that come straight from how the channel is used:

  * **Ephemeral by default.** A count is a staff tool, not channel noise. The caller sees
    the result immediately and alone.
  * **Publishing is a choice.** An admin sets a destination channel; when it is set, the
    same result is also posted there (silently, no ping) so it can be referenced later.
  * **Permission comes from the ladder, not from Discord.** Anyone whose tier holds
    `review` may run it. That is the same authority that approves reports, so the tool
    cannot grant access to someone who could not decide a verdict anyway.
  * **The result is the same text as the CLI and the scheduler.** One renderer, so a
    number cannot mean two things depending on where it is read.
"""

from __future__ import annotations

from typing import Any

import discord
import structlog
from discord import app_commands

from dsbot.modules.quotas.period import current_week, previous_week, weeks_back
from dsbot.modules.quotas.service import CountingService, CountRequest
from dsbot.storage import repos
from dsbot.storage.db import Database

log = structlog.get_logger(__name__)

DEFAULT_TITLE = "Отчёт по рапортам"


class CountCommands(app_commands.Group, name="reports", description="Подсчёт рапортов"):
    """Counting, restricted to people who could already approve reports."""

    def __init__(self, db: Database, resolver: Any = None, timezone: str = "Europe/Moscow") -> None:
        super().__init__()
        self._db = db
        self._resolver = resolver
        # The deployment timezone, used when a channel does not override it. Taken from
        # settings rather than hardcoded, so a slash count and a CLI count of the same
        # period cannot land on different week boundaries.
        self._timezone = timezone

    async def _is_reviewer(self, interaction: discord.Interaction) -> bool:
        """True when the caller holds a tier with the `review` capability.

        Checked in the rank source guild, because that is where the caller's role lives.
        Manage Server is accepted as well: an admin must be able to run the tool even
        before the ladder is filled in, otherwise the feature is unusable on day one.
        """
        perms = getattr(interaction.user, "guild_permissions", None)
        if perms is not None and (perms.manage_guild or perms.administrator):
            return True
        if self._resolver is None:
            return False
        guild_id = await self._rank_guild_for(interaction.channel_id)
        if guild_id is None:
            return False
        ladder_id = await self._ladder_for(interaction.channel_id)
        try:
            rank = await self._resolver.resolve(
                guild_id, interaction.user.id, requires=frozenset({"review"}), ladder_id=ladder_id
            )
        except Exception:
            log.exception("count_permission_check_failed", user_id=interaction.user.id)
            return False
        return bool(rank is not None and rank.can_review)

    async def _ladder_for(self, channel_id: int | None) -> int | None:
        binding = await self._binding(channel_id)
        return binding.rank_ladder_id if binding is not None else None

    async def _binding(self, channel_id: int | None) -> Any:
        if channel_id is None:
            return None
        async with self._db.session() as session:
            return await repos.get_binding_by_channel(session, channel_id)

    async def _rank_guild_for(self, channel_id: int | None) -> int | None:
        binding = await self._binding(channel_id)
        if binding is None:
            return None
        return binding.rank_source_guild_id or binding.guild_id

    @app_commands.command(
        name="count",
        description="Посчитать одобренные рапорты за период",
    )
    @app_commands.describe(
        period="Период: текущая неделя, прошлая неделя или число недель",
        weeks="Сколько последних недель (включая текущую)",
    )
    @app_commands.choices(
        period=[
            app_commands.Choice(name="Текущая неделя", value="current"),
            app_commands.Choice(name="Прошлая неделя", value="last"),
        ]
    )
    async def count(
        self,
        interaction: discord.Interaction,
        period: app_commands.Choice[str] | None = None,
        weeks: int | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        channel_id = interaction.channel_id
        if channel_id is None:
            await interaction.followup.send("Эту команду нужно вызывать в канале.", ephemeral=True)
            return

        if not await self._is_reviewer(interaction):
            await interaction.followup.send(
                "Считать рапорты может только тот, кто вправе их проверять "
                "(роль с правом review) или администратор сервера.",
                ephemeral=True,
            )
            return

        binding = await self._binding(channel_id)
        if binding is None:
            await interaction.followup.send(
                "Этот канал не привязан к форме рапорта.", ephemeral=True
            )
            return

        if weeks is not None and weeks < 1:
            await interaction.followup.send("Число недель должно быть не меньше 1.", ephemeral=True)
            return

        chosen = period.value if period is not None else "current"
        timezone = self._timezone_for(binding)
        window = (
            weeks_back(weeks, timezone=timezone)
            if weeks
            else (
                previous_week(timezone=timezone)
                if chosen == "last"
                else current_week(timezone=timezone)
            )
        )

        config = binding.config or {}
        title = config.get("report_title") or DEFAULT_TITLE
        service = CountingService(self._db)
        request = CountRequest(
            binding_id=binding.id,
            channel_id=channel_id,
            period=window,
            timezone=timezone,
        )

        try:
            outcome = await service.count(
                request,
                title=title,
                scope=f"Канал: <#{channel_id}>",
                persist=True,
            )
        except Exception:
            log.exception("count_command_failed", channel_id=channel_id)
            await interaction.followup.send(
                "Не удалось посчитать рапорты. Подробности в логах бота.", ephemeral=True
            )
            return

        for page in outcome.messages():
            await interaction.followup.send(page, ephemeral=True)
        for problem in outcome.problems:
            await interaction.followup.send(f"⚠️ {problem}", ephemeral=True)

        if outcome.summary.tallies:
            await interaction.followup.send(
                "CSV для таблицы: используйте `/reports csv`.",
                ephemeral=True,
            )

        destination = config.get("report_channel_id")
        if destination:
            posted = await service.publish(request, outcome, target_channel_id=int(destination))
            if posted:
                await interaction.followup.send(
                    f"Также опубликовано в <#{destination}>.", ephemeral=True
                )

        log.info(
            "count_command_done",
            channel_id=channel_id,
            user_id=interaction.user.id,
            period=chosen,
            credited=outcome.summary.credited_total(),
            problems=outcome.problems,
        )

    @app_commands.command(name="csv", description="Выгрузка подсчёта в CSV-файл")
    async def csv(
        self,
        interaction: discord.Interaction,
        period: app_commands.Choice[str] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        channel_id = interaction.channel_id
        if channel_id is None or not await self._is_reviewer(interaction):
            await interaction.followup.send("Недостаточно прав.", ephemeral=True)
            return

        binding = await self._binding(channel_id)
        if binding is None:
            await interaction.followup.send("Канал не привязан к форме.", ephemeral=True)
            return

        chosen = period.value if period is not None else "current"
        timezone = self._timezone_for(binding)
        window = (
            previous_week(timezone=timezone)
            if chosen == "last"
            else current_week(timezone=timezone)
        )
        config = binding.config or {}
        service = CountingService(self._db)
        request = CountRequest(
            binding_id=binding.id,
            channel_id=channel_id,
            period=window,
            timezone=timezone,
        )
        outcome = await service.count(request, title=config.get("report_title") or DEFAULT_TITLE)

        import io

        payload = outcome.csv().encode("utf-8")
        file = discord.File(
            io.BytesIO(payload),
            filename=f"reports-{window.start:%Y-%m-%d}.csv",
        )
        await interaction.followup.send(file=file, ephemeral=True)

    def _timezone_for(self, binding: Any) -> str:
        """Channel override first, then the deployment setting."""
        config = binding.config or {}
        return config.get("timezone") or self._timezone


async def register_count_commands(
    bot: Any, db: Database, resolver: Any = None, timezone: str = "Europe/Moscow"
) -> None:
    bot.tree.add_command(CountCommands(db, resolver, timezone))
    log.info("count_commands_registered")


__all__ = ["CountCommands", "register_count_commands"]
