"""The `/results` command group: when a period closes, and running it by hand.

The admin-facing model is deliberately one number:

    /results set period:week next_result:22.11.2026 23:59:59

That stores "the period ending at this moment is a week long, and the next one ends a week
later". Nothing else is required to make the report publish itself.

Every channel is configured separately, because the two report channels in this community
serve different servers with different rhythms.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import discord
import structlog
from discord import app_commands

from dsbot.modules.quotas.schedule import (
    Schedule,
    describe_close,
    due_at,
    parse_close,
)
from dsbot.storage import repos
from dsbot.storage.db import Database

log = structlog.get_logger(__name__)

PERIOD_CHOICES = [
    app_commands.Choice(name="Неделя", value="week"),
    app_commands.Choice(name="2 недели", value="fortnight"),
    app_commands.Choice(name="Месяц (30 дней)", value="month"),
]

PERIOD_DAYS = {"week": 7, "fortnight": 14, "month": 30}


async def _is_admin(interaction: discord.Interaction) -> bool:
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and (perms.manage_guild or perms.administrator))


class ResultsCommands(app_commands.Group, name="results", description="Итоги периода"):
    """Configure and run a channel's periodic report."""

    def __init__(
        self,
        db: Database,
        *,
        timezone: str = "Europe/Moscow",
        developer_user_id: int | None = None,
    ) -> None:
        super().__init__()
        self._db = db
        self._timezone = timezone
        self._developer_user_id = developer_user_id

    # --- helpers -----------------------------------------------------------

    async def _binding(self, channel_id: int | None) -> Any:
        if channel_id is None:
            return None
        async with self._db.session() as session:
            return await repos.get_binding_by_channel(session, channel_id)

    def _timezone_for(self, binding: Any) -> str:
        config = binding.config or {}
        return config.get("timezone") or self._timezone

    async def _require_admin(self, interaction: discord.Interaction) -> bool:
        if await _is_admin(interaction):
            return True
        await interaction.response.send_message(
            "Менять расписание может только администратор сервера (Manage Server).",
            ephemeral=True,
        )
        return False

    # --- configuration -----------------------------------------------------

    @app_commands.command(
        name="set",
        description="Когда заканчивается период и когда начнётся следующий",
    )
    @app_commands.describe(
        period="Длина периода",
        next_result="Когда период заканчивается, например 22.11.2026 23:59:59",
    )
    @app_commands.choices(period=PERIOD_CHOICES)
    async def set_schedule(
        self,
        interaction: discord.Interaction,
        period: app_commands.Choice[str],
        next_result: str,
    ) -> None:
        """Store this channel's close time.

        The period that gets reported is the one *ending* at `next_result`, so the admin
        writes the moment the current period is over - not the moment it started. That is
        the number people actually have in their heads ("we close on Sunday night").
        """
        if not await self._require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        channel_id = interaction.channel_id
        assert channel_id is not None
        binding = await self._binding(channel_id)
        if binding is None:
            await interaction.followup.send(
                "Этот канал не привязан к форме рапорта, поэтому считать в нём нечего. "
                "Вызовите команду в канале рапортов.",
                ephemeral=True,
            )
            return

        timezone = self._timezone_for(binding)
        try:
            close = parse_close(next_result, timezone)
        except ValueError as exc:
            await interaction.followup.send(f"Не понял дату: {exc}", ephemeral=True)
            return

        days = PERIOD_DAYS.get(period.value, 7)
        schedule = Schedule(period_days=days, next_close=close)
        period_window = schedule.period_for(timezone)

        # A close in the past would publish immediately and then roll forward, which looks
        # like a bug to whoever typed it. Refuse and say what to do instead.
        if close <= datetime.now(UTC):
            await interaction.followup.send(
                f"Эта дата уже прошла ({describe_close(close, timezone)}). "
                "Укажите будущую — или посчитайте прошедший период вручную: "
                "`/reports count` с нужными датами и `publish:yes`.",
                ephemeral=True,
            )
            return

        async with self._db.write_session() as session:
            await repos.set_binding_schedule(
                session, binding_id=binding.id, next_close=close, period_days=days
            )

        destination = (binding.config or {}).get("report_channel_id")
        where = f"<#{destination}>" if destination else "**не выбран** (`/dsbot report-target`)"
        await interaction.followup.send(
            "Расписание сохранено.\n"
            f"**Период:** {period_window.describe(timezone)}\n"
            f"**Закроется:** {describe_close(close, timezone)} ({timezone})\n"
            f"**Следующий:** {describe_close(close + timedelta(days=days), timezone)}\n"
            f"**Публикация в:** {where}",
            ephemeral=True,
        )
        log.info(
            "results_schedule_set",
            channel_id=channel_id,
            period_days=days,
            next_close=close.isoformat(),
            user_id=interaction.user.id,
        )

    @app_commands.command(name="off", description="Отключить автоматические итоги в этом канале")
    async def off(self, interaction: discord.Interaction) -> None:
        if not await self._require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        channel_id = interaction.channel_id
        assert channel_id is not None
        binding = await self._binding(channel_id)
        if binding is None:
            await interaction.followup.send(
                "Этот канал не привязан к форме рапорта.", ephemeral=True
            )
            return
        async with self._db.write_session() as session:
            await repos.clear_binding_schedule(session, binding_id=binding.id)
        await interaction.followup.send(
            "Автоматические итоги в этом канале выключены. Расписание удалено.",
            ephemeral=True,
        )
        log.info("results_schedule_cleared", channel_id=channel_id, user_id=interaction.user.id)

    @app_commands.command(name="show", description="Показать расписание этого канала")
    async def show(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        channel_id = interaction.channel_id
        assert channel_id is not None
        binding = await self._binding(channel_id)
        if binding is None:
            await interaction.followup.send(
                "Этот канал не привязан к форме рапорта.", ephemeral=True
            )
            return

        timezone = self._timezone_for(binding)
        schedule = repos.binding_schedule(binding)
        if schedule is None:
            await interaction.followup.send(
                "Расписание не задано. Включить: `/results set period:week "
                "next_result:22.11.2026 23:59:59`.",
                ephemeral=True,
            )
            return

        now = datetime.now(UTC)
        due = due_at(schedule, now)
        config = binding.config or {}
        destination = config.get("report_channel_id")
        lines = [
            f"**Период:** {schedule.period_for(timezone).describe(timezone)}",
            f"**Длина:** {schedule.period_days} дн.",
            f"**Закроется:** {describe_close(schedule.next_close, timezone)} ({timezone})",
            f"**Следующий:** "
            f"{describe_close(schedule.next_close + schedule.period_length, timezone)}",
            f"**Публикация в:** "
            f"{f'<#{destination}>' if destination else 'не выбрана (`/dsbot report-target`)'}",
        ]
        if due.action == "wait":
            remaining = schedule.next_close - now
            hours = remaining.total_seconds() / 3600
            lines.append(f"**Осталось:** {hours:.1f} ч")
        else:
            lines.append(
                "**Внимание:** время закрытия уже прошло, и бот был выключен в этот "
                "момент. Этот период будет пропущен — посчитайте его вручную командой "
                "`/reports count` с нужными датами."
            )
        await interaction.followup.send("\n".join(lines), ephemeral=True)


async def register_results_commands(
    bot: Any,
    db: Database,
    *,
    timezone: str = "Europe/Moscow",
    developer_user_id: int | None = None,
) -> None:
    bot.tree.add_command(
        ResultsCommands(db, timezone=timezone, developer_user_id=developer_user_id)
    )
    log.info("results_commands_registered")
