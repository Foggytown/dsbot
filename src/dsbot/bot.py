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
from typing import Any

import discord
import structlog

from dsbot.config import Settings
from dsbot.core.bus import EventBus
from dsbot.core.events import (
    MessageCreated,
    MessageDeleted,
    MessageEdited,
    ReactionCleared,
)
from dsbot.discord import sink
from dsbot.discord.reply import DiscordReplySink, ReplySink
from dsbot.ingest import Ingestor
from dsbot.modules.reports.service import ReportService
from dsbot.storage import repos
from dsbot.storage.db import Database

log = structlog.get_logger(__name__)

# Discord requires these to read message text and resolve members across guilds.
INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.members = True
INTENTS.reactions = True


class Bot(discord.Client):
    def __init__(self, settings: Settings, *, reply: ReplySink | None = None) -> None:
        super().__init__(intents=INTENTS)
        self.settings = settings
        self.bus = EventBus()
        self.db = Database(settings)
        self.reply: ReplySink = reply or DiscordReplySink(self)
        self.ingestor = Ingestor(self.db, self.bus)
        self.reports = ReportService(self.db, self.reply)
        self._tasks: set[asyncio.Task[Any]] = set()

    # --- lifecycle ---------------------------------------------------------

    async def setup_hook(self) -> None:
        await self.db.create_all()
        self.bus.subscribe("message_created", self._on_message_created)
        self.bus.subscribe("message_edited", self._on_message_edited)
        log.info(
            "bot_ready",
            database=self.settings.database_url,
            intents=str(self.intents),
        )

    async def close(self) -> None:
        await self.bus.aclose()
        await self.db.dispose()
        await super().close()

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # --- gateway events ----------------------------------------------------

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if message.guild is None:
            return
        if not await self._is_bound(message.channel.id):
            return
        await self.ingestor.handle(sink.message_created(message))

    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        if not await self._is_bound(payload.channel_id):
            return
        channel = self.get_channel(payload.channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            return
        try:
            message = await channel.fetch_message(payload.message_id)  # type: ignore[attr-defined]
        except discord.HTTPException:
            log.warning("edit_fetch_failed", message_id=payload.message_id)
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
        result = await self.reports.process_message(
            event.message.channel_id, event.message.author_id, event.message.message_id
        )
        if result is not None:
            log.info(
                "report_processed",
                report_id=result.report_id,
                matched=result.matched,
                valid=result.valid,
                action=result.action,
                issues=result.issues,
            )

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

    bot = Bot(settings)
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    with contextlib.suppress(NotImplementedError):
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)

    runner = asyncio.create_task(bot.start(settings.discord_token))
    await stop.wait()
    log.info("shutdown_requested")
    await bot.close()
    runner.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await runner


__all__ = ["INTENTS", "Bot", "run_bot"]
