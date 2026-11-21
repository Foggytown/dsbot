"""The staff-facing slash command: count reports on demand, for any period.

Design constraints that come straight from how the channel is used:

  * **Any period.** Current week, last week, the last N days/weeks, or two explicit dates.
    A weekly report is the common case, but "how did November go" must not require a shell.
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

import io
from typing import Any

import discord
import structlog
from discord import app_commands

from dsbot.modules.quotas.period import Period, resolve_period
from dsbot.modules.quotas.service import CountingService, CountRequest
from dsbot.storage import repos
from dsbot.storage.db import Database

log = structlog.get_logger(__name__)

DEFAULT_TITLE = "Отчёт по рапортам"

# Discord allows at most 25 choices per option.
PERIOD_CHOICES = [
    app_commands.Choice(name="Текущая неделя", value="current"),
    app_commands.Choice(name="Прошлая неделя", value="last"),
]

SOURCE_CHOICES = [
    app_commands.Choice(name="История Discord (рекомендуется)", value="discord"),
    app_commands.Choice(name="База бота (только с момента запуска)", value="db"),
]


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

    def _timezone_for(self, binding: Any) -> str:
        """Channel override first, then the deployment setting."""
        config = binding.config or {}
        return config.get("timezone") or self._timezone

    def _window(
        self,
        binding: Any,
        *,
        period: app_commands.Choice[str] | None,
        weeks: int | None,
        days: int | None,
        start: str | None,
        end: str | None,
    ) -> Period:
        """Resolve the window once, so `count` and `csv` cannot disagree.

        Raises ValueError with a readable sentence, which the caller shows to staff.
        """
        return resolve_period(
            timezone=self._timezone_for(binding),
            start=start,
            end=end,
            days=days,
            weeks=weeks,
            which=period.value if period is not None else "current",
        )

    @app_commands.command(
        name="count",
        description="Посчитать одобренные рапорты за любой период",
    )
    @app_commands.describe(
        period="Готовая неделя: текущая или прошлая",
        weeks="Или: сколько последних недель, включая текущую",
        days="Или: сколько последних дней",
        start="Или: начало периода, ДД.ММ.ГГГГ или ГГГГ-ММ-ДД",
        end="Конец периода, включительно (указывать вместе с start)",
        source="Откуда читать: история Discord (по умолчанию) или база бота",
    )
    @app_commands.choices(period=PERIOD_CHOICES, source=SOURCE_CHOICES)
    async def count(
        self,
        interaction: discord.Interaction,
        period: app_commands.Choice[str] | None = None,
        weeks: int | None = None,
        days: int | None = None,
        start: str | None = None,
        end: str | None = None,
        source: app_commands.Choice[str] | None = None,
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

        try:
            window = self._window(
                binding, period=period, weeks=weeks, days=days, start=start, end=end
            )
        except ValueError as exc:
            await interaction.followup.send(f"Не понял период: {exc}", ephemeral=True)
            return

        chosen_source = source.value if source is not None else "discord"
        config = binding.config or {}
        title = config.get("report_title") or DEFAULT_TITLE
        timezone = self._timezone_for(binding)

        try:
            if chosen_source == "discord":
                pages, problems, counted = await self._live_report(
                    interaction=interaction,
                    channel_id=channel_id,
                    window=window,
                    title=title,
                    timezone=timezone,
                )
            else:
                pages, problems, counted = await self._db_report(
                    channel_id, binding, window, title, timezone
                )
        except Exception:
            log.exception("count_command_failed", channel_id=channel_id)
            await interaction.followup.send(
                "Не удалось посчитать рапорты. Подробности в логах бота.", ephemeral=True
            )
            return

        for page in pages:
            await interaction.followup.send(page, ephemeral=True)
        for problem in problems:
            await interaction.followup.send(f"⚠️ {problem}", ephemeral=True)

        if counted:
            await interaction.followup.send(
                "Файл для таблицы: `/reports csv` с тем же периодом.", ephemeral=True
            )

        destination = config.get("report_channel_id")
        if destination and pages:
            posted = 0
            for page in pages:
                if await self._send_to(interaction, int(destination), page):
                    posted += 1
            if posted:
                await interaction.followup.send(
                    f"Также опубликовано в <#{destination}>.", ephemeral=True
                )

        log.info(
            "count_command_done",
            channel_id=channel_id,
            user_id=interaction.user.id,
            source=chosen_source,
            start=window.start.isoformat(),
            end=window.end.isoformat(),
        )

    async def _live_report(
        self,
        *,
        interaction: discord.Interaction,
        channel_id: int,
        window: Period,
        title: str,
        timezone: str,
    ) -> tuple[list[str], list[str], bool]:
        """Read the real channel history and count it.

        This is the default because the database only knows what the bot was running for;
        a weekly report over a period the bot never watched would otherwise be empty.
        """
        from dsbot.modules.quotas.live import count_channel_from_discord
        from dsbot.modules.quotas.render import RenderContext, render_message

        channel = interaction.channel
        if channel is None:
            raise RuntimeError("no channel on the interaction")

        result = await count_channel_from_discord(
            channel=channel,
            db=self._db,
            channel_id=channel_id,
            period=window,
            resolver=self._resolver,
        )
        context = RenderContext(
            title=title,
            period_label=result.period.describe(timezone),
            timezone=timezone,
            scope=f"Канал: <#{channel_id}> · история Discord",
        )
        pages = render_message(result.summary, context)
        problems = list(result.problems)
        if result.stats.matched == 0 and result.stats.messages:
            problems.append(
                f"В канале {result.stats.messages} сообщений за период, но ни одно не "
                "распознано как рапорт. Проверьте, что форма канала совпадает с тем, "
                "как люди пишут."
            )
        return pages, problems, bool(result.summary.tallies)

    async def _db_report(
        self,
        channel_id: int,
        binding: Any,
        window: Period,
        title: str,
        timezone: str,
    ) -> tuple[list[str], list[str], bool]:
        """Count from what the bot stored. Faster, but only covers uptime."""
        service = CountingService(self._db)
        request = CountRequest(
            binding_id=binding.id,
            channel_id=channel_id,
            period=window,
            timezone=timezone,
        )
        outcome = await service.count(
            request,
            title=title,
            scope=f"Канал: <#{channel_id}> · база бота",
            persist=False,
        )
        return outcome.messages(), list(outcome.problems), bool(outcome.summary.tallies)

    async def _send_to(self, interaction: discord.Interaction, target: int, content: str) -> bool:
        """Post into another channel, resolving it if it is not cached."""
        client = interaction.client
        destination = client.get_channel(target)
        if destination is None:
            try:
                destination = await client.fetch_channel(target)
            except Exception:
                log.exception("publish_channel_fetch_failed", channel_id=target)
                return False
        if not isinstance(destination, discord.abc.Messageable):
            log.error("publish_target_not_messageable", channel_id=target)
            return False
        try:
            await destination.send(content, allowed_mentions=discord.AllowedMentions(users=True))
        except Exception:
            log.exception("publish_send_failed", channel_id=target)
            return False
        return True

    @app_commands.command(name="csv", description="Выгрузка подсчёта в CSV-файл")
    @app_commands.describe(
        period="Готовая неделя: текущая или прошлая",
        weeks="Или: сколько последних недель",
        days="Или: сколько последних дней",
        start="Или: начало периода, ДД.ММ.ГГГГ",
        end="Конец периода, включительно",
        source="Откуда читать: история Discord или база бота",
    )
    @app_commands.choices(period=PERIOD_CHOICES, source=SOURCE_CHOICES)
    async def csv(
        self,
        interaction: discord.Interaction,
        period: app_commands.Choice[str] | None = None,
        weeks: int | None = None,
        days: int | None = None,
        start: str | None = None,
        end: str | None = None,
        source: app_commands.Choice[str] | None = None,
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

        try:
            window = self._window(
                binding, period=period, weeks=weeks, days=days, start=start, end=end
            )
        except ValueError as exc:
            await interaction.followup.send(f"Не понял период: {exc}", ephemeral=True)
            return

        from dsbot.modules.quotas.render import render_csv

        config = binding.config or {}
        chosen_source = source.value if source is not None else "discord"

        if chosen_source == "discord":
            from dsbot.modules.quotas.live import count_channel_from_discord

            result = await count_channel_from_discord(
                channel=interaction.channel,
                db=self._db,
                channel_id=channel_id,
                period=window,
                resolver=self._resolver,
            )
            payload_text = render_csv(result.summary)
        else:
            service = CountingService(self._db)
            request = CountRequest(
                binding_id=binding.id,
                channel_id=channel_id,
                period=window,
                timezone=self._timezone_for(binding),
            )
            outcome = await service.count(
                request, title=config.get("report_title") or DEFAULT_TITLE
            )
            payload_text = outcome.csv()

        file = discord.File(
            io.BytesIO(payload_text.encode("utf-8")),
            filename=f"reports-{window.start:%Y-%m-%d}_{window.end:%Y-%m-%d}.csv",
        )
        await interaction.followup.send(file=file, ephemeral=True)


async def register_count_commands(
    bot: Any, db: Database, resolver: Any = None, timezone: str = "Europe/Moscow"
) -> None:
    bot.tree.add_command(CountCommands(db, resolver, timezone))
    log.info("count_commands_registered")


__all__ = ["CountCommands", "register_count_commands"]
