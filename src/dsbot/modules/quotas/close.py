"""Closing a period: count it from Discord, publish it, and advance the schedule.

One function, `close_period`, does the whole job for one channel:

  1. take the period that just ended
  2. count it from Discord history (not the archive - see AGENTS.md)
  3. publish into the channel's `report_channel_id`, editing an earlier copy if present
  4. roll the schedule forward, but only if the publish succeeded

Step 4 is last on purpose. If publishing fails, the schedule must not move: a failed run
should be retried on the next tick, not silently recorded as "this week is done".

Why this does not touch the archive: counting reads Discord, and that decision is already
settled. A scheduler that counted the database would report only what the bot happened to
see, which on a panel that restarts is the same class of bug as the one that made a
"last week" count return five reports.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from dsbot.modules.quotas.schedule import DEFAULT_GRACE, Due, due_at
from dsbot.storage import repos
from dsbot.storage.db import Database

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class CloseOutcome:
    """What a close attempt did, for the log and for the slash command."""

    action: str  # published | skipped | failed | nothing
    channel_id: int
    period_label: str = ""
    pages: int = 0
    destination: int | None = None
    problem: str = ""
    next_close: datetime | None = None
    people: int = 0

    @property
    def ok(self) -> bool:
        return self.action in {"published", "skipped", "nothing"}


async def close_period(
    *,
    db: Database,
    client: Any,
    binding_id: int,
    now: datetime | None = None,
    grace: timedelta = DEFAULT_GRACE,
    force: bool = False,
    advance: bool = True,
    developer_user_id: int | None = None,
    reply: Any = None,
) -> CloseOutcome:
    """Close one channel's period if it is due.

    `force` publishes regardless of the clock, which is what an admin uses to run a period
    by hand after the bot skipped one. In that mode the schedule is
    advanced only when the period is the one the schedule points at; a forced run of an
    arbitrary past period must not move the current schedule.
    """
    moment = now or datetime.now(UTC)

    async with db.session() as session:
        binding = await repos.get_binding_by_id(session, binding_id)
        if binding is None:
            return CloseOutcome(action="failed", channel_id=0, problem="binding not found")
        schedule = repos.binding_schedule(binding)
        config = dict(binding.config or {})
        report_channel = config.get("report_channel_id")
        timezone = config.get("timezone") or "Europe/Moscow"
        channel_id = binding.channel_id

    if schedule is None:
        return CloseOutcome(
            action="nothing",
            channel_id=channel_id,
            problem="this channel has no close schedule",
        )

    due = due_at(schedule, moment, grace=grace, timezone=timezone)
    if force:
        # A manual run always counts the period the schedule points at.
        due = Due(
            action="publish",
            period=schedule.period_for(timezone),
            next_close=schedule.next_close,
        )
    elif due.action == "wait":
        return CloseOutcome(action="nothing", channel_id=channel_id)

    assert due.period is not None
    label = due.period.describe(timezone)

    if due.action == "skip":
        log.warning(
            "period_close_skipped_too_late",
            channel_id=channel_id,
            period=label,
            skipped=due.skipped_periods,
            hint="the bot was not running at the close; an admin can run it by hand",
        )
        if advance and due.next_close is not None:
            async with db.write_session() as session:
                await repos.set_binding_schedule(
                    session,
                    binding_id=binding_id,
                    next_close=due.next_close,
                    period_days=schedule.period_days,
                )
        return CloseOutcome(
            action="skipped",
            channel_id=channel_id,
            period_label=label,
            next_close=due.next_close,
            problem="period closed while the bot was offline",
        )

    if report_channel is None:
        return CloseOutcome(
            action="failed",
            channel_id=channel_id,
            period_label=label,
            problem=(
                "nowhere to publish: set a destination with /dsbot report-target. "
                "The schedule was not advanced."
            ),
        )

    channel = await _resolve_channel(client, channel_id)
    if channel is None:
        return CloseOutcome(
            action="failed",
            channel_id=channel_id,
            period_label=label,
            problem="the report channel is not reachable",
        )

    resolver = await _build_resolver(db, client)

    try:
        counted = await count_period(
            db=db,
            channel=channel,
            channel_id=channel_id,
            period=due.period,
            resolver=resolver,
        )
    except Exception:
        log.exception("period_close_count_failed", channel_id=channel_id, period=label)
        return CloseOutcome(
            action="failed",
            channel_id=channel_id,
            period_label=label,
            problem="counting failed; the schedule was not advanced",
        )

    destination = int(report_channel)
    # Check access BEFORE counting. A missing permission is a configuration mistake, and
    # counting first would spend a minute of Discord requests only to discover it cannot
    # publish. It also must not advance the schedule.
    unreachable = await _destination_problem(client, destination)
    if unreachable:
        return CloseOutcome(
            action="failed",
            channel_id=channel_id,
            period_label=label,
            destination=destination,
            problem=unreachable,
        )

    try:
        posted, publish_error = await publish_period(
            db=db,
            client=client,
            binding_id=binding_id,
            channel_id=channel_id,
            period=due.period,
            counted=counted,
            target_channel_id=destination,
            timezone=timezone,
            hidden_tiers=tuple(str(v) for v in (config.get("hidden_tiers") or [])),
            developer_user_id=developer_user_id,
            reply=reply,
        )
    except Exception as exc:
        log.exception("period_close_publish_failed", channel_id=channel_id, period=label)
        return CloseOutcome(
            action="failed",
            channel_id=channel_id,
            period_label=label,
            destination=destination,
            problem=f"публикация не удалась: {exc}. Расписание не сдвинуто.",
        )

    if not posted:
        return CloseOutcome(
            action="failed",
            channel_id=channel_id,
            period_label=label,
            destination=destination,
            # Say WHY. "nothing was posted" is exactly the kind of message that sends
            # someone reading logs instead of fixing a permission.
            problem=(
                publish_error
                or f"не удалось отправить отчёт в <#{destination}>. Расписание не сдвинуто."
            ),
        )

    next_close = due.next_close
    if advance and next_close is not None:
        async with db.write_session() as session:
            await repos.set_binding_schedule(
                session,
                binding_id=binding_id,
                next_close=next_close,
                period_days=schedule.period_days,
            )

    log.info(
        "period_closed",
        channel_id=channel_id,
        period=label,
        destination=destination,
        pages=len(posted),
        people=len(counted.summary.tallies),
        next_close=next_close.isoformat() if next_close else None,
    )
    return CloseOutcome(
        action="published",
        channel_id=channel_id,
        period_label=label,
        pages=len(posted),
        destination=destination,
        next_close=next_close,
        people=len(counted.summary.tallies),
    )


async def count_period(
    *,
    db: Database,
    channel: Any,
    channel_id: int,
    period: Any,
    resolver: Any,
) -> Any:
    """Count one period from Discord history. Shared by the scheduler and the manual close."""
    from dsbot.modules.quotas.live import count_channel_from_discord

    return await count_channel_from_discord(
        channel=channel,
        db=db,
        channel_id=channel_id,
        period=period,
        resolver=resolver,
    )


async def publish_period(
    *,
    db: Database,
    client: Any,
    binding_id: int,
    channel_id: int,
    period: Any,
    counted: Any,
    target_channel_id: int,
    timezone: str,
    hidden_tiers: tuple[str, ...] = (),
    developer_user_id: int | None = None,
    reply: Any = None,
) -> tuple[list[int], str]:
    """Render, persist and publish one counted period.

    **`counted` is the report.** It comes from `count_channel_from_discord`, which reads the
    real channel history and the real reactions - the same path `/reports count` uses.

    This used to ignore `counted` and call `CountingService.count()` instead, which reads
    the *database*. That produced a visibly different report: every report showed as
    unreviewed, because the archive only knows reactions the bot happened to see live. The
    fix is not to re-count - it is to publish the count that was already made.

    `CountingService.count` is still called, but only with `persist=True` to write the
    audit row, and its summary is discarded in favour of `counted`.

    `reply` is injectable so the whole path can be exercised without a gateway connection.
    """
    from dsbot.discord.reply import DiscordReplySink
    from dsbot.modules.quotas.render import RenderContext
    from dsbot.modules.quotas.service import CountingService, CountOutcome, CountRequest

    context = RenderContext(
        period_label=period.describe(timezone),
        timezone=timezone,
        scope=f"Канал: <#{channel_id}> · история Discord",
        hidden_tiers=hidden_tiers,
        developer_user_id=developer_user_id,
    )
    sink = reply if reply is not None else DiscordReplySink(client)
    service = CountingService(db, sink, developer_user_id=developer_user_id)
    request = CountRequest(
        binding_id=binding_id,
        channel_id=channel_id,
        period=period,
        timezone=timezone,
    )
    # Persist the run so `publish` can attach the message ids to it, which is what makes a
    # re-run edit the same message instead of posting a second one.
    persisted = await service.count(request, persist=True, hidden_tiers=hidden_tiers)

    # The published report must be the Discord count, not the database recount above.
    outcome = CountOutcome(
        summary=counted.summary,
        context=context,
        run_id=persisted.run_id,
        roster_size=len(counted.summary.tallies),
        problems=list(getattr(counted, "problems", []) or []),
    )
    posted = await service.publish(request, outcome, target_channel_id=target_channel_id)
    return posted, getattr(sink, "last_error", "") or ""


async def _build_resolver(db: Database, client: Any) -> Any:
    """A resolver wired to this client and the stored ladders."""
    from dsbot.modules.ranks.resolver import RankResolver
    from dsbot.modules.ranks.source import DiscordRankSource
    from dsbot.storage import ranks_repo

    resolver = RankResolver(DiscordRankSource(client), {})
    async with db.session() as session:
        resolver.set_ladders(await ranks_repo.load_all_ladders(session))
    return resolver


async def _resolve_channel(client: Any, channel_id: int) -> Any:
    """The channel, from cache or via REST.

    `get_channel` only sees cached channels and returned None for a perfectly reachable
    channel in an earlier bug, which made every send a silent no-op. Always fall back.
    """
    channel = client.get_channel(channel_id)
    if channel is not None:
        return channel
    try:
        return await client.fetch_channel(channel_id)
    except Exception:
        log.warning("close_channel_fetch_failed", channel_id=channel_id)
        return None


async def _destination_problem(client: Any, channel_id: int) -> str:
    """Can the bot actually post here? Returns "" when it can.

    Probed by sending a throwaway message and deleting it, because computing permissions
    needs the Members intent and gets channel overwrites wrong. A stored destination the
    bot cannot write to is the worst failure mode: the setting looks saved, every report
    then fails, and only the log knows.
    """
    channel = await _resolve_channel(client, channel_id)
    if channel is None:
        return f"канал <#{channel_id}> не найден или бот его не видит"
    send = getattr(channel, "send", None)
    if send is None:
        return f"<#{channel_id}> — это не текстовый канал, писать в него нельзя"
    try:
        probe = await send(
            "dsbot: проверка прав публикации (удалится само)",
            allowed_mentions=None,
        )
    except Exception as exc:
        if _is_forbidden(exc):
            return (
                f"нет доступа к <#{channel_id}>: боту нужно право «Просматривать канал» "
                f"и «Отправлять сообщения»"
            )
        return f"не удалось проверить <#{channel_id}>: {exc}"
    with contextlib.suppress(Exception):
        await probe.delete()
    return ""


def _is_forbidden(exc: BaseException) -> bool:
    try:
        import discord

        return isinstance(exc, discord.Forbidden)
    except Exception:  # pragma: no cover - discord is always installed in production
        return False
