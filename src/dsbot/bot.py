"""The live bot.

Wiring only: settings -> database -> bus -> sink -> modules. No domain logic lives here,
which is why the same handlers can be driven from a backfill dump in tests.

Run with:
    uv run dsbot run
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import discord
import discord.ext.commands
import structlog

from dsbot.config import Settings
from dsbot.core.bus import EventBus
from dsbot.core.events import (
    MessageCreated,
    MessageDeleted,
    MessageEdited,
    ReactionCleared,
)
from dsbot.core.lock import InstanceLock
from dsbot.discord import sink
from dsbot.discord.reply import DiscordReplySink, ReplySink
from dsbot.ingest import Ingestor
from dsbot.jobs.retry import RetryScheduler
from dsbot.modules.reports.service import ReportService
from dsbot.storage import repos
from dsbot.storage.db import Database

log = structlog.get_logger(__name__)

# Intents come from settings so the required portal toggles match what is implemented.
# Message Content is always on (reports are text). Members is opt-in until ranks exist.


class Bot(discord.ext.commands.Bot):
    def __init__(self, settings: Settings, *, reply: ReplySink | None = None) -> None:
        super().__init__(
            command_prefix="!",
            intents=settings.intents(),
            # Slash commands are the admin surface; prefix commands are unused.
            help_command=None,
        )
        self.settings = settings
        self.bus = EventBus()
        self.db = Database(settings)
        self.reply: ReplySink = reply or DiscordReplySink(self)
        self.ingestor = Ingestor(self.db, self.bus)
        self.reports = ReportService(
            self.db,
            self.reply,
            group_window=timedelta(seconds=settings.report_group_window_seconds),
            allow_bot_authors=settings.dev_accept_bot_messages,
            contact_user_id=settings.contact_user_id,
        )
        self.retry = RetryScheduler(self.reports)
        self.reports.attach_retry(self.retry)
        self._tasks: set[asyncio.Task[Any]] = set()

    # --- lifecycle ---------------------------------------------------------

    @property
    def user_id_if_ready(self) -> int | None:
        return self.user.id if self.user is not None else None

    async def setup_hook(self) -> None:
        await self.db.create_all()
        self.bus.subscribe("message_created", self._on_message_created)
        self.bus.subscribe("message_edited", self._on_message_edited)

        from dsbot.discord.commands import register_commands

        await register_commands(self, self.db)

        # A restart between "deferred" and its deadline loses the in-memory timer, and
        # the report would then never resolve. Sweep them at startup.
        from dsbot.jobs.replay import expire_deferred

        try:
            resolved = await expire_deferred(self.db, self.reports)
            if resolved:
                log.info("deferred_recovered_at_startup", count=resolved)
        except Exception:
            log.exception("deferred_recovery_failed")

        bindings = await self._load_bindings()
        if not bindings and (self.settings.channels_file or self.settings.auto_bind_channels):
            # A fresh deployment has an empty database. Panels often offer no shell, so
            # the channel list has to come from configuration instead.
            bindings = await self._auto_bind()

        if not bindings:
            # Silence with no explanation is the worst failure mode: the bot is running,
            # the user sees nothing, and there is no clue why. Say it plainly.
            log.error(
                "no_channel_bindings",
                hint=(
                    "No channel is bound, so every message is ignored. Bind one by either:\n"
                    "  - running: dsbot form-apply --channel <id> --file "
                    "forms/training.yaml --guild <id>\n"
                    "  - or setting DSBOT_AUTO_BIND_CHANNELS=<guild_id>:<channel_id> in .env"
                ),
            )
        else:
            for binding in bindings:
                log.info(
                    "binding_active",
                    channel_id=binding.channel_id,
                    guild_id=binding.guild_id,
                    module=binding.module_key,
                )
            await self._sync_commands(bindings)

        log.info(
            "bot_ready",
            database=self.settings.database_url,
            intents=str(self.intents),
            bindings=len(bindings),
            group_window_seconds=self.settings.report_group_window_seconds,
        )

    async def _load_bindings(self) -> list[Any]:
        async with self.db.session() as session:
            return list(await repos.list_enabled_bindings(session))

    async def _auto_bind(self) -> list[Any]:
        """Make the database match `forms/channels.yaml`, then reload.

        The channels file is configuration and lives in git, so a channel set up locally
        is deployed by pushing. Idempotent: an existing binding keeps its state, including
        a deliberate pause, unless the file says otherwise.
        """
        from dsbot.modules.reports.channels import load_channels_file
        from dsbot.modules.reports.channels_sync import sync_channels

        try:
            spec = load_channels_file(Path(self.settings.channels_file))
        except Exception:
            log.exception("channels_file_unreadable", path=self.settings.channels_file)
            return await self._load_bindings()

        if not spec.channels:
            log.error(
                "channels_file_empty",
                path=self.settings.channels_file,
                hint="Add channels to that file, or set DSBOT_AUTO_BIND_CHANNELS.",
            )
            return await self._load_bindings()

        try:
            async with self.db.write_session() as session:
                result = await sync_channels(session, spec)
        except Exception:
            log.exception("channels_sync_failed", path=self.settings.channels_file)
            return await self._load_bindings()

        log.info(
            "channels_synced",
            created=result.created,
            updated=result.updated,
            paused=result.paused,
            unchanged=len(result.unchanged),
        )
        return await self._load_bindings()

    async def _sync_commands(self, bindings: list[Any]) -> None:
        """Push slash commands to each guild that has a binding.

        Guild-scoped sync is instant, unlike global sync which can take an hour. That
        matters: an admin should not be told "wait up to an hour" after a fix.
        """
        guild_ids = {b.guild_id for b in bindings if b.guild_id}
        for guild_id in guild_ids:
            guild = discord.Object(id=guild_id)
            try:
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                log.info("slash_commands_synced", guild_id=guild_id, count=len(synced))
            except discord.HTTPException:
                log.exception("slash_command_sync_failed", guild_id=guild_id)

    async def close(self) -> None:
        await self.retry.aclose()
        await self.bus.aclose()
        await self.db.dispose()
        await super().close()

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # --- gateway events ----------------------------------------------------

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot and not (
            self.settings.dev_accept_bot_messages and message.author.id == self.user_id_if_ready
        ):
            return
        if message.guild is None:
            log.debug("dm_ignored", user_id=message.author.id)
            return
        if not await self._is_bound(message.channel.id):
            log.debug("unbound_channel_ignored", channel_id=message.channel.id)
            return
        log.debug("message_ingesting", message_id=message.id, channel_id=message.channel.id)
        await self.ingestor.handle(sink.message_created(message))

    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        if not await self._is_bound(payload.channel_id):
            return
        # get_channel only sees cached channels; fetch is the reliable path and this
        # used to fail intermittently for exactly that reason.
        channel = self.get_channel(payload.channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(payload.channel_id)
            except discord.HTTPException:
                log.warning("edit_channel_fetch_failed", channel_id=payload.channel_id)
                return
        if not isinstance(channel, discord.abc.Messageable):
            return
        try:
            message = await channel.fetch_message(payload.message_id)  # type: ignore[attr-defined]
        except discord.NotFound:
            # Deleted between the edit and this fetch; nothing to store.
            log.debug("edit_message_gone", message_id=payload.message_id)
            return
        except discord.HTTPException as exc:
            log.warning("edit_fetch_failed", message_id=payload.message_id, error=str(exc))
            return
        if message.author.bot:
            return
        await self.ingestor.handle(sink.message_edited(None, message))

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if not await self._is_bound(payload.channel_id):
            return
        await self.ingestor.handle(sink.message_deleted(payload))

    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        if not await self._is_bound(payload.channel_id):
            return
        for message_id in payload.message_ids:
            await self.ingestor.handle(
                MessageDeleted(
                    message_id=message_id,
                    channel_id=payload.channel_id,
                    guild_id=payload.guild_id,
                )
            )

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if not await self._is_bound(payload.channel_id):
            return
        await self.ingestor.handle(sink.reaction_added(payload))

    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        if not await self._is_bound(payload.channel_id):
            return
        await self.ingestor.handle(sink.reaction_removed(payload))

    async def on_raw_reaction_clear(self, payload: discord.RawReactionClearEvent) -> None:
        if not await self._is_bound(payload.channel_id):
            return
        # One gateway event clears every emoji; expand it so the reaction table stays
        # truthful without requiring a re-fetch per emoji.
        async with self.db.session() as session:
            existing = await repos.list_reactions(session, payload.message_id)
        for reaction in existing:
            await self.ingestor.handle(
                ReactionCleared(
                    channel_id=payload.channel_id,
                    guild_id=payload.guild_id,
                    message_id=payload.message_id,
                    emoji=_emoji_from_key(reaction.emoji_key, reaction.emoji_id),
                )
            )

    async def on_raw_reaction_clear_emoji(
        self, payload: discord.RawReactionClearEmojiEvent
    ) -> None:
        if not await self._is_bound(payload.channel_id):
            return
        await self.ingestor.handle(sink.reaction_cleared(payload))

    # --- handlers ----------------------------------------------------------

    async def _on_message_created(self, event: MessageCreated) -> None:
        snapshot = event.message
        # A bare photo usually completes an earlier report, so the reports still
        # waiting for this author must be re-examined. Their own group cannot see
        # backwards, which is why this cannot be handled by the new message alone.
        if not (snapshot.content or "").strip() and snapshot.attachments:
            await self._complete_pending(snapshot)
            return

        result = await self.reports.process_message(
            snapshot.channel_id, snapshot.author_id, snapshot.message_id
        )
        if result is not None:
            log.info(
                "report_processed",
                report_id=result.report_id,
                author=snapshot.author_name,
                author_id=snapshot.author_id,
                matched=result.matched,
                valid=result.valid,
                action=result.action,
                issues=result.issues,
            )

    async def _complete_pending(self, snapshot: Any) -> None:
        pending = await self.reports.pending_for_author(
            snapshot.channel_id, snapshot.author_id, snapshot.message_id
        )
        if not pending:
            return
        for message_id in pending:
            result = await self.reports.process_message(
                snapshot.channel_id, snapshot.author_id, message_id
            )
            if result is None:
                continue
            log.info(
                "report_rechecked",
                report_id=result.report_id,
                author=snapshot.author_name,
                author_id=snapshot.author_id,
                triggered_by=snapshot.message_id,
                action=result.action,
                valid=result.valid,
                issues=result.issues,
            )
            if result.valid:
                break

    async def _on_message_edited(self, event: MessageEdited) -> None:
        # An edit can complete a report, so re-parse the same group.
        await self.reports.process_message(
            event.message.channel_id, event.message.author_id, event.message.message_id
        )

    # --- helpers -----------------------------------------------------------

    async def _is_bound(self, channel_id: int) -> bool:
        async with self.db.session() as session:
            binding = await repos.get_binding_by_channel(session, channel_id)
        return binding is not None


def _emoji_from_key(key: str, emoji_id: int | None) -> Any:
    from dsbot.core.events import EmojiRef

    if key.startswith("custom:"):
        return EmojiRef(id=emoji_id or int(key.split(":", 1)[1]), name=None)
    return EmojiRef(id=None, name=key.split(":", 1)[1] if ":" in key else key)


async def run_bot(settings: Settings) -> None:
    if not settings.discord_token:
        raise RuntimeError("DSBOT_DISCORD_TOKEN is empty; put your token in .env")

    # One instance per lock file. Two bots on one database look like random behaviour
    # and corrupt state, so refuse to start rather than misbehave. The lock is named per
    # instance, so a test bot can run next to the live one with its own database.
    lock = InstanceLock(settings.lock_file())
    try:
        lock.acquire()
    except RuntimeError as exc:
        typer_echo_error(str(exc))
        raise SystemExit(1) from None

    bot = Bot(settings)
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    with contextlib.suppress(NotImplementedError):
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)

    try:
        runner = asyncio.create_task(bot.start(settings.discord_token))
        await stop.wait()
        log.info("shutdown_requested")
        await bot.close()
        runner.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await runner
    finally:
        lock.release()


def typer_echo_error(message: str) -> None:
    """Print to stderr without importing click at module scope."""
    print(message, file=sys.stderr)


__all__ = ["Bot", "run_bot"]
