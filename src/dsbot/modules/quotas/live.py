"""Counting straight from Discord, with no database in the middle.

Why this exists: the database only holds what the bot happened to see while it was
running. Ask it for "last week" when the bot started yesterday and the honest answer is
"nothing", which looks identical to "nobody reported". Discord itself is the only source
of truth for history.

So this path reads the channel's real messages for the requested window, parses them with
the same parser the live bot uses, reads each message's reactions, and counts. Nothing is
written: no messages, no reports, no verdicts. That makes it safe to run for a whole year
and safe to run on a live channel.

Three things have to be joined, exactly as the live pipeline does:

  * **message groups** - a report plus its follow-up photos by the same author
  * **reactions** - who approved or rejected, and their rank in the rank source guild
  * **the roster** - who was expected to report, and how many

The roster is the one piece that still comes from stored state, because "everyone with
role X" cannot be reconstructed for a past date from Discord: roles are current-only. So
the roster is scraped now and counted as it stands, which is stated in the output.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

import structlog

from dsbot.modules.quotas.counting import (
    CountedReport,
    CountSummary,
    RosterEntry,
    evaluate_reports,
    tally,
)
from dsbot.modules.quotas.period import Period
from dsbot.modules.reports.parser import GroupMessage, ReportCandidate, parse_report
from dsbot.modules.reports.schema import FormSchemaDef
from dsbot.modules.verdict.engine import Rank, Reaction

log = structlog.get_logger(__name__)

# Discord returns at most 100 messages per request.
PAGE = 100

# How many reaction reads to keep in flight. Each `reaction.users()` is one REST call and
# they are independent, so doing them sequentially was the entire cost of a scan. This is
# deliberately modest: fanning out too wide earns 429s, which is slower than being steady.
CONCURRENT_READS = 8


@dataclass(slots=True)
class LiveScanStats:
    messages: int = 0
    groups: int = 0
    matched: int = 0
    invalid: int = 0
    reactions: int = 0
    unresolved_reactors: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "messages": self.messages,
            "groups": self.groups,
            "matched": self.matched,
            "invalid": self.invalid,
            "reactions": self.reactions,
            "unresolved_reactors": self.unresolved_reactors,
        }


@dataclass(slots=True)
class LiveCountResult:
    summary: CountSummary
    stats: LiveScanStats
    period: Period
    problems: list[str] = field(default_factory=list)
    # Tier labels to leave out of the message, so a noisy tier can be hidden per channel.
    hidden_tiers: tuple[str, ...] = ()
    # When set, only this tier is shown.
    only_tier: str | None = None


class HistoryChannel(Protocol):
    """The only thing this module needs from a channel.

    discord.py's `history()` returns an async iterator synchronously (it is not awaited),
    so typing it as a coroutine would be wrong even though the checker cannot tell from
    `Any`.
    """

    def history(self, **kwargs: Any) -> AsyncIterator[Any]: ...


async def iter_messages(
    channel: HistoryChannel,
    period: Period,
    *,
    on_page: Any = None,
) -> AsyncIterator[Any]:
    """Yield messages inside `[start, end)` from real Discord history.

    Reads the window Discord gives cheaply and stops at its edges, so a one-week report
    over a busy channel costs a few requests rather than the whole history.
    """
    count = 0
    async for message in channel.history(limit=None, after=period.start, before=period.end):
        count += 1
        yield message
        if on_page is not None and count % PAGE == 0:
            on_page(count)


def group_messages(messages: list[Any], *, max_gap_seconds: float = 120.0) -> list[list[Any]]:
    """Join a report with the attachment-only messages that follow it.

    The rule is the live one: same author, no text, within the gap window. A message with
    text starts a group of its own, because joining it would let one report steal the
    photos of the next. Getting this wrong is how a valid report reads as "no photos".
    """
    groups: list[list[Any]] = []
    for message in sorted(messages, key=lambda m: m.created_at):
        if not groups:
            groups.append([message])
            continue
        previous = groups[-1][-1]
        same_author = previous.author.id == message.author.id
        empty_text = not (message.content or "").strip()
        has_attachment = bool(message.attachments)
        gap = (message.created_at - previous.created_at).total_seconds()
        if same_author and empty_text and has_attachment and gap <= max_gap_seconds:
            groups[-1].append(message)
            continue
        groups.append([message])
    return groups


def _group_message(message: Any) -> GroupMessage:
    return GroupMessage(
        message_id=message.id,
        author_id=message.author.id,
        created_at=message.created_at,
        content=message.content or "",
        attachment_count=len(message.attachments),
        author_name=getattr(message.author, "display_name", None) or message.author.name,
    )


async def count_from_discord(
    *,
    channel: Any,
    schemas: list[FormSchemaDef],
    period: Period,
    roster: list[RosterEntry],
    resolver: Any,
    rank_guild_id: int,
    review_ladder_id: int | None = None,
    stats_out: LiveScanStats | None = None,
) -> tuple[CountSummary, LiveScanStats]:
    """Parse live history for one period and count approved reports per person.

    No database writes. A reactor whose rank cannot be resolved is counted as unable to
    review, never as allowed, for the same reason as the live path: a NULL must never
    grant approval.
    """
    stats = stats_out or LiveScanStats()

    collected: list[Any] = []
    async for message in iter_messages(channel, period):
        stats.messages += 1
        if message.author.bot:
            continue
        collected.append(message)

    groups = group_messages(collected)
    stats.groups = len(groups)

    # Pass 1: parse everything (pure, microseconds).
    prepared: list[tuple[Any, list[Any]]] = []
    for group in groups:
        candidate = ReportCandidate(messages=[_group_message(m) for m in group])
        attachments = sum(len(m.attachments) for m in group)
        parsed = parse_report(schemas, candidate, attachment_count=attachments)
        if not parsed.matched:
            continue
        stats.matched += 1
        if not parsed.is_valid:
            stats.invalid += 1
        prepared.append((group, []))

    # Pass 2: read reactions concurrently. This is where essentially all the time goes:
    # `reaction.users()` is one REST call per emoji per message, so doing it in a loop is
    # what made a scan take half a minute. Bounded so we stay inside the rate limit.
    reads = await _gather_bounded(
        [_read_reactions(group) for group, _ in prepared], limit=CONCURRENT_READS
    )

    reactor_ids: set[int] = set()
    all_reactions: list[tuple[list[Reaction], list[int]]] = []
    for reactions, reactors in reads:
        stats.reactions += len(reactions)
        reactor_ids.update(reactors)
        all_reactions.append((reactions, reactors))

    # Pass 3: resolve each distinct reactor once, concurrently.
    resolved = await _resolve_ranks(resolver, rank_guild_id, review_ladder_id, reactor_ids)
    stats.unresolved_reactors = sum(1 for value in resolved.values() if value is None)

    outcomes = []
    for (group, _), (reactions, _reactors) in zip(prepared, all_reactions, strict=True):
        ranks: dict[int, Rank | None] = {r.user_id: resolved.get(r.user_id) for r in reactions}
        outcomes.append(
            CountedReport(
                report_id=group[0].id,
                author_user_id=group[0].author.id,
                created_at=group[0].created_at,
                reactions=tuple(reactions),
                ranks=ranks,
            )
        )

    summary = tally(evaluate_reports(outcomes), roster)
    log.info("live_count_done", period=period.label(), **stats.as_dict())
    return summary, stats


async def _gather_bounded(coros: list[Any], *, limit: int) -> list[Any]:
    """Run coroutines with at most `limit` in flight, preserving result order.

    Discord allows a handful of concurrent requests; going wide is how the scan went from
    tens of seconds to a few. The bound matters more than the ceiling: an unbounded
    gather over a busy channel would earn 429s and be slower in the end.
    """
    import asyncio as _asyncio

    semaphore = _asyncio.Semaphore(limit)

    async def one(coro: Any) -> Any:
        async with semaphore:
            return await coro

    return list(await _asyncio.gather(*(one(coro) for coro in coros)))


APPROVE = {"\u2705", "\u2714", "\u2611"}
REJECT = {"\u274c", "\u2716", "\u26d4"}

# Discord sends the count with each reaction. Above this many reactors we still need the
# list to know who they were, but a single count tells us nothing about identity, so the
# count is only used to skip emoji we would ignore anyway.


async def _read_reactions(group: list[Any]) -> tuple[list[Reaction], list[int]]:
    """Collect approve/reject reactions on a report's messages. No rank lookups here.

    Separated from rank resolution so all the rank calls can be batched and run together.
    """
    reactions: list[Reaction] = []
    reactor_ids: list[int] = []

    for message in group:
        for reaction in message.reactions:
            name = getattr(reaction.emoji, "name", None) or str(reaction.emoji)
            if name not in APPROVE and name not in REJECT:
                continue
            async for user in reaction.users(limit=100):
                if getattr(user, "bot", False):
                    continue
                reactions.append(Reaction(user_id=user.id, emoji_name=name))
                reactor_ids.append(user.id)
    return reactions, reactor_ids


async def _resolve_ranks(
    resolver: Any,
    guild_id: int,
    ladder_id: int | None,
    user_ids: set[int],
) -> dict[int, Rank | None]:
    """Resolve every distinct reactor's review authority, concurrently.

    The resolver caches per user, so repeat runs over overlapping periods cost nothing.
    `gather` is safe: one failure marks that reactor unknown instead of failing the report.
    """
    if resolver is None or not user_ids:
        return {uid: None for uid in user_ids}

    import asyncio as _asyncio

    async def one(user_id: int) -> tuple[int, Rank | None]:
        return user_id, await _rank_of(resolver, guild_id, user_id, ladder_id)

    results = await _asyncio.gather(*(one(uid) for uid in user_ids))
    return dict(results)


async def count_channel_from_discord(
    *,
    channel: Any,
    db: Any,
    channel_id: int,
    period: Period,
    resolver: Any,
    only_tier: str | None = None,
) -> LiveCountResult:
    """Everything the slash command needs, resolved from stored config but read from Discord.

    Stored config (the form, the ladder, the roster) is configuration, not history, so it
    is read from the database. The reports themselves are read from Discord, because the
    database only knows what the bot happened to capture.
    """
    from dsbot.modules.ranks.resolver import GuildLadders
    from dsbot.storage import ranks_repo, repos

    problems: list[str] = []

    async with db.session() as session:
        binding = await repos.get_binding_by_channel(session, channel_id)
        if binding is None:
            return LiveCountResult(
                summary=CountSummary(),
                stats=LiveScanStats(),
                period=period,
                problems=["this channel is not bound to a report form"],
            )
        schema_rows = await repos.get_schemas_for_binding(session, binding.id)
        if not schema_rows:
            return LiveCountResult(
                summary=CountSummary(),
                stats=LiveScanStats(),
                period=period,
                problems=["no form is bound to this channel, so nothing can be parsed"],
            )
        from dsbot.modules.reports.schema import from_definition

        schemas = [from_definition(row.definition) for row in schema_rows]

        guild_id = binding.rank_source_guild_id or binding.guild_id
        ladders: dict[int, GuildLadders] = await ranks_repo.load_all_ladders(session)
        group = ladders.get(guild_id)

        roster: list[RosterEntry] = []
        config = binding.config or {}
        quota_ladder_id = _quota_ladder_id(group, config)
        if quota_ladder_id is not None:
            stored = await repos.list_roster(session, quota_ladder_id)
            overrides = await repos.get_quota_policies(
                session, ladder_id=quota_ladder_id, binding_id=binding.id
            )
            ordinals = await repos.get_tier_ordinals(session, quota_ladder_id)
            override_by_ordinal = _required_by_tier(overrides, ordinals)
            roster = [
                RosterEntry(
                    user_id=row.user_id,
                    display_name=row.display_name,
                    tier_label=row.tier_label,
                    tier_ordinal=row.tier_ordinal,
                    required_count=override_by_ordinal.get(
                        row.tier_ordinal, row.required_per_week or 0
                    ),
                )
                for row in stored
            ]
            if not roster:
                problems.append(
                    "the roster is empty, so there is nobody to compare against. "
                    "Run `dsbot roster-refresh --channel <id>` once."
                )
        else:
            problems.append(
                "no quota ladder is configured for this channel, so there are no "
                "weekly norms to compare against. Reports are still counted."
            )

        # Rows grouped by tier are only useful if the tier is the quota one; a review
        # ladder's ordinals would mislabel people who owe reports.
        hidden = [str(value) for value in (config.get("hidden_tiers") or [])]
        review_ladder_id = binding.rank_ladder_id

    summary, stats = await count_from_discord(
        channel=channel,
        schemas=schemas,
        period=period,
        roster=roster,
        resolver=resolver,
        rank_guild_id=guild_id,
        review_ladder_id=review_ladder_id,
    )
    return LiveCountResult(
        summary=summary,
        stats=stats,
        period=period,
        problems=problems,
        hidden_tiers=tuple(hidden),
        only_tier=only_tier,
    )


def _quota_ladder_id(group: Any, config: dict[str, Any]) -> int | None:
    from dsbot.modules.quotas.service import _quota_ladder_id as shared

    return shared(group, config)


def _required_by_tier(policies: Any, tiers: dict[int, int] | None = None) -> dict[Any, int]:
    from dsbot.modules.quotas.service import _required_by_tier as shared

    return shared(policies, tiers)


async def _rank_of(
    resolver: Any, guild_id: int, user_id: int, ladder_id: int | None
) -> Rank | None:
    if resolver is None:
        return None
    try:
        resolved = await resolver.resolve(
            guild_id, user_id, requires=frozenset({"review"}), ladder_id=ladder_id
        )
    except Exception:
        log.exception("live_count_rank_failed", user_id=user_id)
        return None
    if resolved is None:
        return None
    return Rank(user_id=user_id, ordinal=resolved.ordinal or 0, tier_label=resolved.tier_label)


__all__ = [
    "LiveCountResult",
    "LiveScanStats",
    "count_channel_from_discord",
    "count_from_discord",
    "group_messages",
    "iter_messages",
]
