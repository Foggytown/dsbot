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
from datetime import UTC, datetime, timedelta
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
    ReactionAdded,
    ReactionCleared,
    ReactionRemoved,
)
from dsbot.core.lock import InstanceLock
from dsbot.discord import sink
from dsbot.discord.reply import DiscordReplySink, ReplySink
from dsbot.ingest import Ingestor
from dsbot.modules.ranks.resolver import GuildLadders, RankResolver
from dsbot.modules.ranks.roster import DiscordRosterSource, RosterService
from dsbot.modules.ranks.source import DiscordRankSource
from dsbot.modules.reports.service import ReportService
from dsbot.modules.verdict.service import VerdictService
from dsbot.storage import repos
from dsbot.storage.db import Database

log = structlog.get_logger(__name__)

# How often the channels file is checked for edits. One `stat` per interval: frequent
# enough that an admin sees their change within a few seconds, cheap enough to ignore.
CHANNELS_POLL_SECONDS = 5.0

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
        # Ranks are resolved from the rapporteur's home guild, so the same object serves
        # both verdicts and roster scraping.
        self.rank_source = DiscordRankSource(self)
        self.resolver = RankResolver(
            self.rank_source,
            {},
        )
        self.roster = RosterService(self.db, DiscordRosterSource(self))
        self.verdicts = VerdictService(self.db, self.resolver)
        self._tasks: set[asyncio.Task[Any]] = set()
        self._ladders: dict[int, GuildLadders] = {}

    # --- lifecycle ---------------------------------------------------------

    @property
    def user_id_if_ready(self) -> int | None:
        return self.user.id if self.user is not None else None

    async def setup_hook(self) -> None:
        await self.db.create_all()
        applied = await self.db.prepare()
        if applied:
            log.warning(
                "schema_upgraded_on_start",
                columns=applied,
                hint="Existing rows keep their old values; new columns start empty.",
            )
        self.bus.subscribe("message_created", self._on_message_created)
        self.bus.subscribe("message_edited", self._on_message_edited)
        self.bus.subscribe("reaction_added", self._on_reaction_added)
        self.bus.subscribe("reaction_removed", self._on_reaction_removed)
        self.bus.subscribe("reaction_cleared", self._on_reaction_cleared)

        await self._load_ladders()

        from dsbot.discord.commands import register_commands

        await register_commands(self, self.db)

        from dsbot.discord.count_commands import register_count_commands

        await register_count_commands(
            self,
            self.db,
            self.resolver,
            self.settings.default_timezone,
            developer_user_id=self.settings.developer_user_id,
        )

        # Reports are warned about immediately now, but older rows may still carry the
        # retired "deferred" status from before that change. Resolve them on start so a
        # database migrated from an older version does not keep silent leftovers.
        from dsbot.jobs.replay import expire_deferred

        try:
            resolved = await expire_deferred(self.db, self.reports)
            if resolved:
                log.info("stale_deferred_resolved_at_startup", count=resolved)
        except Exception:
            log.exception("deferred_recovery_failed")

        bindings = await self._load_bindings()
        # ALWAYS reconcile with the channels file. The panel keeps only /data across a
        # redeploy and replaces the rest of the checkout, so a channel added to the file
        # must be applied on every start or it would appear to "not work" until someone
        # wiped the database. Sync is idempotent, so running it unconditionally is safe.
        #
        # Do NOT gate this on `settings.channels_file` being set: it defaults to empty
        # because the real path is derived from the data directory. Gating on it made the
        # condition False on a fresh panel, so no channel was ever bound and the bot
        # started silently serving nothing. That shipped and cost a deployment.
        bindings = await self._auto_bind()
        # Ladders may just have been created by the sync, so reload them afterwards.
        # Without this, a ladder applied by the file would be invisible to approvals
        # until the next restart, which on a panel means "it did not work".
        await self._load_ladders()

        if not bindings:
            # Silence with no explanation is the worst failure mode: the bot is running,
            # the user sees nothing, and there is no clue why. Say it plainly.
            log.error(
                "no_channel_bindings",
                path=str(self.settings.resolved_channels_file()),
                hint=(
                    "No channel is bound, so every message is ignored. Add one to the "
                    "channels file on the server (the path above) and it is applied "
                    "within a few seconds."
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
            database=self.settings.resolved_database_url,
            intents=str(self.intents),
            bindings=len(bindings),
            channels_file=str(self.settings.resolved_channels_file()),
            group_window_seconds=self.settings.report_group_window_seconds,
        )
        self._spawn(self._watch_channels_file())

    async def _load_bindings(self) -> list[Any]:
        async with self.db.session() as session:
            return list(await repos.list_enabled_bindings(session))

    async def _watch_channels_file(self) -> None:
        """Apply an edit to channels.yaml while the bot is running.

        Editing the file on the server and waiting for a restart is workable but feels
        broken: the change is live in the file and not in the bot. This polls the
        modification time, which is one `stat` every few seconds and behaves the same on
        Windows and in a container, and re-applies only when it actually changed.

        A broken edit is logged and ignored: the previous configuration keeps running,
        because dropping every channel on a YAML typo would be worse than being stale.
        """
        from dsbot.modules.reports.channels import file_signature

        path = self.settings.resolved_channels_file()
        last = file_signature(path)
        while True:
            await asyncio.sleep(CHANNELS_POLL_SECONDS)
            if self.is_closed():
                return
            current = file_signature(path)
            if current is None or current == last:
                continue
            last = current
            log.info("channels_file_changed", path=str(path))
            try:
                await self._auto_bind()
            except Exception:
                log.exception("channels_reload_failed", path=str(path))

    async def _auto_bind(self) -> list[Any]:
        """Make the database match the channels file, then reload.

        The live file lives in the data directory (the panel's /data), not in the
        checkout, so an admin can edit it on the server without a redeploy. The checked-in
        `forms/channels.yaml` is copied there once, on first start, and never again:
        otherwise a redeploy would overwrite the admin's edits.

        Idempotent: an existing binding keeps its state, including a deliberate pause,
        unless the file says otherwise.
        """
        from dsbot.modules.reports.channels import load_channels_file, seed_channels_file
        from dsbot.modules.reports.channels_sync import sync_channels

        path = self.settings.resolved_channels_file()
        try:
            if seed_channels_file(path, self.settings.seed_file()):
                log.info("channels_file_seeded", path=str(path))
        except Exception:
            log.exception("channels_seed_failed", path=str(path))

        try:
            spec = load_channels_file(path)
        except Exception:
            log.exception("channels_file_unreadable", path=str(path))
            return await self._load_bindings()

        if not spec.channels:
            log.error(
                "channels_file_empty",
                path=str(path),
                hint="Add channels to that file on the server, or to forms/channels.yaml "
                "before the first start.",
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
            ladders=result.ladders,
            forms=result.forms,
        )
        return await self._load_bindings()

    async def _sync_commands(self, bindings: list[Any]) -> None:
        """Push the current command tree to every guild the bot is in.

        Guild-scoped sync is instant, unlike global sync which can take an hour. That
        matters: an admin should not be told "wait up to an hour" after a fix.

        Deliberately not limited to guilds that have a binding. A guild can keep a *stale*
        registration from an earlier deploy, and if we skip it, Discord keeps offering
        commands this process cannot resolve - which surfaces as
        `CommandNotFound: Application command 'reports' not found` in the log, triggered by
        a user clicking an old menu entry. Syncing every guild overwrites the stale
        registration with what actually exists now.
        """
        guild_ids = {b.guild_id for b in bindings if b.guild_id}
        # Also cover guilds without a binding: they may hold a stale registration.
        guild_ids.update(guild.id for guild in self.guilds)

        if not guild_ids:
            log.warning(
                "slash_commands_not_synced",
                hint="the bot is in no guild yet; commands sync on the next start",
            )

        for guild_id in guild_ids:
            guild = discord.Object(id=guild_id)
            try:
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                log.info(
                    "slash_commands_synced",
                    guild_id=guild_id,
                    count=len(synced),
                    commands=[command.name for command in synced],
                )
            except discord.HTTPException:
                log.exception("slash_command_sync_failed", guild_id=guild_id)

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
        now = datetime.now(UTC)
        async with self.db.session() as session:
            existing = await repos.list_reactions(session, payload.message_id)
        for reaction in existing:
            await self.ingestor.handle(
                ReactionCleared(
                    channel_id=payload.channel_id,
                    guild_id=payload.guild_id,
                    message_id=payload.message_id,
                    emoji=_emoji_from_key(reaction.emoji_key, reaction.emoji_id),
                    cleared_at=now,
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

    # --- reactions ---------------------------------------------------------

    async def _on_reaction_added(self, event: ReactionAdded) -> None:
        """Store the reaction with a rank snapshot, then recompute the verdict.

        The snapshot is the whole point: a reaction arrives from a user with no roles
        (they are not a member of the report guild), so the tier must be resolved from
        their home guild once, now, and frozen. Recomputing later must not apply a
        ladder that has since changed.
        """
        if event.guild_id is None:
            return
        binding = await self._binding_for(event.channel_id)
        if binding is None:
            return

        rank_guild = binding.rank_source_guild_id or binding.guild_id
        rank = await self._resolve_rank(rank_guild, event.user_id, ladder_id=binding.rank_ladder_id)

        await self._store_reaction(
            event,
            tier_key=rank.tier_key if rank else None,
            tier_ordinal=rank.ordinal if rank else None,
            tier_label=rank.tier_label if rank else None,
            can_review=rank.can_review if rank else None,
        )
        await self._recompute_verdict(event.message_id, event.user_id, rank)

    async def _on_reaction_removed(self, event: ReactionRemoved) -> None:
        async with self.db.write_session() as session:
            await repos.remove_reaction(
                session,
                message_id=event.message_id,
                emoji_key=event.emoji.key,
                user_id=event.user_id,
            )
        await self._recompute_verdict(event.message_id, event.user_id, None)

    async def _on_reaction_cleared(self, event: ReactionCleared) -> None:
        async with self.db.write_session() as session:
            await repos.remove_reaction_emoji(
                session, message_id=event.message_id, emoji_key=event.emoji.key
            )
        await self._recompute_verdict(event.message_id, None, None)

    async def _store_reaction(
        self,
        event: ReactionAdded,
        *,
        tier_key: str | None,
        tier_ordinal: int | None,
        tier_label: str | None,
        can_review: bool | None,
    ) -> None:
        async with self.db.write_session() as session:
            await repos.add_reaction(
                session,
                message_id=event.message_id,
                channel_id=event.channel_id,
                guild_id=event.guild_id,
                emoji_key=event.emoji.key,
                emoji_id=event.emoji.id,
                emoji_name=event.emoji.name,
                user_id=event.user_id,
                tier_key=tier_key,
                tier_ordinal=tier_ordinal,
                tier_label=tier_label,
                can_review=can_review,
            )

    async def _recompute_verdict(self, message_id: int, user_id: int | None, rank: Any) -> None:
        update = await self.verdicts.on_reaction(message_id)
        if update is None:
            log.debug("reaction_on_non_report", message_id=message_id)
            return
        if rank is None and user_id is not None:
            log.info(
                "reaction_rank_unresolved",
                message_id=message_id,
                user_id=user_id,
                verdict=update.verdict,
            )

    async def _resolve_rank(
        self, guild_id: int, user_id: int, *, ladder_id: int | None = None
    ) -> Any:
        """Resolve a reactor's *review* authority, tolerating Discord being unreachable.

        `requires={"review"}` is deliberate. Quota tiers (who must file reports) and
        review tiers (who may approve) are different things, and a role that carries only
        a quota must never be able to decide a verdict. Asking for the capability up front
        makes the ladder decide that, not the order of roles in the file.

        `ladder_id` pins the answer to the channel's own ladder when the binding names
        one, so two channels in one guild can order the same people differently.

        A missing rank is not fatal: the reaction is still stored, it simply cannot
        contribute to a verdict. Dropping the reaction would lose state and make a later
        un-react behave wrongly.
        """
        try:
            return await self.resolver.resolve(
                guild_id,
                user_id,
                requires=frozenset({"review"}),
                ladder_id=ladder_id,
            )
        except Exception:
            log.exception("rank_resolve_failed", guild_id=guild_id, user_id=user_id)
            return None

    async def _binding_for(self, channel_id: int) -> Any:
        async with self.db.session() as session:
            return await repos.get_binding_by_channel(session, channel_id)

    async def _load_ladders(self) -> None:
        """Load ladders into the resolver. A config change needs a restart, as with forms."""
        from dsbot.storage import ranks_repo

        try:
            async with self.db.session() as session:
                ladders = await ranks_repo.load_all_ladders(session)
        except Exception:
            log.exception("ladder_load_failed")
            return

        self._ladders = ladders
        self.resolver.set_ladders(ladders)
        if not ladders:
            log.warning(
                "no_rank_ladders",
                hint="No ladder is loaded, so no reaction can approve anything. "
                "Add the ladders to forms/ladders/ and name them in channels.yaml.",
            )
            return
        for guild_id, group in ladders.items():
            review_roles = sum(1 for caps in group.by_role.values() if caps and "review" in caps)
            log.info(
                "ladders_loaded",
                guild_id=guild_id,
                ladders=[index.key for index in group.indexes],
                roles=len(group.by_role),
                review_roles=review_roles,
                quota_ladder=group.quota_ladder_id,
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

    # Refuse to start with a database outside the data directory. On a panel that file is
    # deleted on the next upload, and the failure is invisible until someone notices that
    # months of history are gone.
    location_problem = settings.database_location_problem()
    if location_problem:
        typer_echo_error(location_problem)
        raise SystemExit(1)

    location_warning = settings.database_location_warning()
    if location_warning:
        log.warning("database_outside_data_dir", detail=location_warning)

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
