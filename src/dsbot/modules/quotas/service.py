"""The counting service: database + roster + pure rules -> a result, and optionally a post.

Design notes that matter:

  * **Counting reads, never writes verdicts.** The verdict stored on a report is the one
    the verdict service computed from reactions. Counting recomputes from the same
    stored reaction snapshots so a report that was never live-processed (backfilled, or
    reacted to while the bot was down) still gets an honest answer.
  * **A quota is optional.** With no policy configured the count is still produced; only
    the `met` marker disappears. Reporting a number is useful on its own, and requiring a
    quota before the first count would block the tool on configuration nobody has set yet.
  * **Re-running a period is idempotent.** A stored run is reused and its results and
    message are replaced, so a channel never accumulates duplicates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from dsbot.modules.quotas.counting import (
    CountedReport,
    CountSummary,
    RosterEntry,
    evaluate_reports,
    tally,
)
from dsbot.modules.quotas.period import Period
from dsbot.modules.quotas.render import RenderContext, render_csv, render_message
from dsbot.modules.ranks.resolver import GuildLadders
from dsbot.storage import repos
from dsbot.storage.db import Database

if TYPE_CHECKING:
    from dsbot.discord.reply import ReplySink

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class CountRequest:
    binding_id: int
    channel_id: int
    period: Period
    timezone: str = "Europe/Moscow"


@dataclass(slots=True)
class CountOutcome:
    """Everything a caller - slash command, CLI or scheduler - needs."""

    summary: CountSummary
    context: RenderContext
    run_id: int | None = None
    roster_size: int = 0
    problems: list[str] = field(default_factory=list)
    degraded: bool = False
    # A roster problem is a configuration gap, not a failure to produce numbers: the
    # count is still valid for everyone who actually reported.
    roster_missing: bool = False

    def messages(self) -> list[str]:
        return render_message(self.summary, self.context)

    def csv(self) -> str:
        return render_csv(self.summary)

    @property
    def ok(self) -> bool:
        return not self.degraded


class CountingService:
    def __init__(self, db: Database, reply: ReplySink | None = None) -> None:
        self._db = db
        self._reply = reply

    async def count(
        self,
        request: CountRequest,
        *,
        ladders: GuildLadders | None = None,
        quota_ladder_id: int | None = None,
        title: str = "Отчёт по рапортам",
        scope: str = "",
        persist: bool = False,
    ) -> CountOutcome:
        """Count one period for one binding.

        `ladders` is the guild's whole ladder set, not one ladder: the population that
        owes reports and the population that may approve are described by different
        ladders, and counting must not confuse them.
        """
        problems: list[str] = []

        binding = await self._load_binding(request.binding_id)
        if binding is None:
            raise ValueError(f"binding {request.binding_id} not found")

        if ladders is None and binding.rank_source_guild_id:
            ladders = await self._ladders_for(binding.rank_source_guild_id)
        if quota_ladder_id is None:
            quota_ladder_id = _quota_ladder_id(ladders, binding.config or {})

        roster, roster_problem = await self._roster_entries(quota_ladder_id, ladders, binding.id)
        if roster_problem:
            problems.append(roster_problem)

        reports, reaction_rows = await self._load_reports(request)
        counted = _to_counted(reports, reaction_rows)
        outcomes = evaluate_reports(counted)
        summary = tally(outcomes, roster)

        context = RenderContext(
            title=title,
            period_label=request.period.describe(request.timezone),
            timezone=request.timezone,
            generated_at=datetime.now(UTC),
            scope=scope,
        )
        outcome = CountOutcome(
            summary=summary,
            context=context,
            roster_size=len(roster),
            problems=problems,
            degraded=bool(problems),
            roster_missing=roster_problem is not None,
        )

        log.info(
            "count_done",
            binding_id=request.binding_id,
            channel_id=request.channel_id,
            period=f"{request.period.start.isoformat()}..{request.period.end.isoformat()}",
            reports=len(outcomes),
            roster=len(roster),
            credited=summary.credited_total(),
            pending=summary.pending_total(),
            problems=problems,
        )

        if persist:
            outcome.run_id = await self._persist(request, outcome)
        return outcome

    async def publish(
        self, request: CountRequest, outcome: CountOutcome, *, target_channel_id: int
    ) -> list[int]:
        """Post the report, replacing an earlier copy of the same period if present.

        Replacement is by message, not by deletion: an existing page is edited in place
        when possible. That keeps the report where staff already look and stops a re-run
        from leaving a trail of near-identical messages.
        """
        if self._reply is None:
            log.warning("count_not_published_no_sink", binding_id=request.binding_id)
            return []

        pages = outcome.messages()
        existing = await self._existing_message_ids(request)
        posted: list[int] = []

        for index, page in enumerate(pages):
            previous = existing[index] if index < len(existing) else None
            if previous is not None and await self._reply.edit(target_channel_id, previous, page):
                posted.append(previous)
                continue
            message_id = await self._reply.send(target_channel_id, page)
            if message_id is None:
                log.error(
                    "count_publish_failed",
                    binding_id=request.binding_id,
                    channel_id=target_channel_id,
                    page=index,
                )
                return posted
            posted.append(message_id)

        # A shorter report must not leave orphaned pages from a previous, longer run.
        for stale in existing[len(pages) :]:
            if await self._reply.clear(target_channel_id, stale):
                log.info("count_publish_extra_page_removed", channel_id=target_channel_id)

        if posted and outcome.run_id is not None:
            async with self._db.write_session() as session:
                await repos.set_run_report_message(
                    session,
                    outcome.run_id,
                    channel_id=target_channel_id,
                    message_ids=posted,
                )
        log.info(
            "count_published",
            binding_id=request.binding_id,
            channel_id=target_channel_id,
            messages=len(posted),
        )
        return posted

    async def history(self, binding_id: int, *, limit: int = 12) -> list[dict[str, Any]]:
        """Recent stored runs, newest first, for a quick look at previous weeks."""
        async with self._db.session() as session:
            runs = await repos.list_runs_for_binding(session, binding_id, limit=limit)
            return [
                {
                    "run_id": run.id,
                    "start": run.period_start,
                    "end": run.period_end,
                    "status": run.status,
                    "summary": run.summary or {},
                }
                for run in runs
            ]

    # --- internals ---------------------------------------------------------

    async def _load_binding(self, binding_id: int) -> Any:
        async with self._db.session() as session:
            return await repos.get_binding_by_id(session, binding_id)

    async def _ladders_for(self, guild_id: int) -> GuildLadders | None:
        from dsbot.storage import ranks_repo

        async with self._db.session() as session:
            ladders = await ranks_repo.load_all_ladders(session)
        return ladders.get(guild_id)

    async def _roster_entries(
        self, ladder_id: int | None, ladders: GuildLadders | None, binding_id: int
    ) -> tuple[list[RosterEntry], str | None]:
        """Stored roster plus requirements, or a problem explaining why it is empty.

        The requirement comes from the quota ladder and was frozen onto the roster row at
        scrape time. A per-channel override in `quota_policies` wins when present, which
        is the only reason that table still exists.
        """
        if ladder_id is None:
            return [], (
                "no quota ladder is configured for this channel's rank source guild, "
                "so the list of people to count is unknown"
            )

        async with self._db.session() as session:
            stored = await repos.list_roster(session, ladder_id)
            overrides = await repos.get_quota_policies(
                session, ladder_id=ladder_id, binding_id=binding_id
            )
            tier_ordinals = await repos.get_tier_ordinals(session, ladder_id)

        problem: str | None = None
        if not stored:
            problem = (
                "the roster is empty: nobody has been scraped yet, or no member holds a "
                "configured role. Run `dsbot roster-refresh --channel <id>` to scrape it."
            )

        override_by_ordinal = _required_by_tier(overrides, tier_ordinals)
        entries = [
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
        if ladders is not None and not entries:
            problem = problem or f"nobody in guild {ladders.guild_id} holds a quota role"
        return entries, problem

    async def _load_reports(self, request: CountRequest) -> tuple[list[Any], list[Any]]:
        async with self._db.session() as session:
            reports = await repos.list_reports_for_binding_between(
                session,
                binding_id=request.binding_id,
                start=request.period.start,
                end=request.period.end,
            )
            message_ids = [r.message_id for r in reports]
            reactions = await repos.list_reactions_for_messages(session, message_ids)
        return list(reports), list(reactions)

    async def _persist(self, request: CountRequest, outcome: CountOutcome) -> int:
        summary = outcome.summary
        payload = {
            "roster_size": outcome.roster_size,
            "credited": summary.credited_total(),
            "pending": summary.pending_total(),
            "conflict": summary.conflict_total(),
            "problems": outcome.problems,
        }
        async with self._db.write_session() as session:
            run = await repos.put_run(
                session,
                binding_id=request.binding_id,
                period_start=request.period.start,
                period_end=request.period.end,
                status="complete" if outcome.ok else "degraded",
                summary=payload,
            )
            rows = [
                {
                    "user_id": row.user_id,
                    "display_name": row.display_name,
                    "tier_id": None,
                    "tier_label": row.tier_label,
                    "tier_ordinal": row.tier_ordinal,
                    "required_count": row.required_count,
                    "approved_count": row.approved_count,
                    "pending_count": row.pending_count,
                    "rejected_count": row.rejected_count,
                    "conflict_count": row.conflict_count,
                    "met": row.met,
                }
                for row in summary.tallies
            ]
            await repos.replace_run_results(session, run.id, rows)
            return run.id

    async def _existing_message_ids(self, request: CountRequest) -> list[int]:
        """Message ids of this period's earlier report, so a re-run can edit them."""
        async with self._db.session() as session:
            run = await repos.get_run_by_period(
                session,
                binding_id=request.binding_id,
                period_start=request.period.start,
                period_end=request.period.end,
            )
            if run is None:
                return []
            stored = [int(i) for i in (run.published_ids or []) if i]
        if stored:
            return stored
        return [run.report_message_id] if run.report_message_id else []


def _quota_ladder_id(ladders: GuildLadders | None, config: dict[str, Any]) -> int | None:
    """Which ladder this channel counts against.

    The channel names its quota ladder in `forms/channels.yaml`, so this is a per-channel
    choice, not a global one. When it is not named, the single quota ladder of the guild
    is used; if the guild has several and the channel names none, counting refuses rather
    than guessing which duty applies.
    """
    if ladders is None:
        return None
    wanted = config.get("quota_ladder")
    if wanted:
        for index in ladders.indexes:
            if index.key == wanted and index.purpose == "quota":
                return index.ladder_id
        return None
    quota_ladders = [index for index in ladders.indexes if index.purpose == "quota"]
    if len(quota_ladders) == 1:
        return quota_ladders[0].ladder_id
    return None


def _required_by_tier(policies: Any, tiers: dict[int, int] | None = None) -> dict[int | None, int]:
    """Required count per tier ordinal.

    Quota policies store a `tier_id`; a count only knows the ordinal from the roster
    snapshot. `tiers` maps tier id to ordinal so the two can be joined without a second
    query, and the ordinal is what staff actually recognise in the report.
    """
    mapping = tiers or {}
    required: dict[int | None, int] = {}
    for policy in policies:
        ordinal = mapping.get(policy.tier_id) if policy.tier_id is not None else None
        # The first policy wins, and the repo orders a binding-specific policy first.
        required.setdefault(ordinal, policy.required_count)
    return required


def _to_counted(reports: list[Any], reactions: list[Any]) -> list[CountedReport]:
    """Join stored reports with their reactions, carrying the rank snapshots."""
    from dsbot.modules.verdict.service import _snapshot

    by_message: dict[int, list[Any]] = {}
    for row in reactions:
        by_message.setdefault(row.message_id, []).append(row)

    counted: list[CountedReport] = []
    for report in reports:
        rows = by_message.get(report.message_id, [])
        snapshot = _snapshot(rows)
        counted.append(
            CountedReport(
                report_id=report.id,
                author_user_id=report.author_user_id,
                created_at=report.created_at,
                reactions=tuple(snapshot.reactions),
                ranks=snapshot.ranks,
            )
        )
    return counted


__all__ = ["CountOutcome", "CountRequest", "CountingService"]
