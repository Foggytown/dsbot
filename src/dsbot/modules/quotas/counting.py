"""Counting approved reports per person.

The rules, settled with the community:

  * A report counts for its **author**. Attribution to a mentioned person is a later
    feature, so this counts what was actually written without pretending to know more.
  * A report counts when its verdict is `approved`. The verdict itself is computed from
    reactions by the verdict engine, and only a tier with the `review` capability can
    produce one. Who may review is therefore ladder configuration, not a count setting.
  * Anything still undecided at the moment of counting counts as **approved**. The
    reasoning is explicit: an unreviewed report is the reviewers' backlog, and the
    instructor who filed it on time must not be penalised for someone else's delay.
    Undecided reports are reported separately as `pending_count` so a full backlog is
    still visible instead of being hidden inside the approved total.
  * `conflict` never counts. Equal authority disagreeing is a configuration or staff
    problem, and guessing a side would produce a wrong number.

This module is pure: it takes reports and reactions, and returns counts. No database, no
Discord, no clock. That keeps every rule above testable in isolation.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from dsbot.modules.verdict.engine import Rank, Reaction, VerdictResult, compute_verdict

# Verdicts that credit the author with an approval.
CREDITED_VERDICTS: frozenset[str] = frozenset({"approved", "pending"})
REJECTED = "rejected"
CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class CountedReport:
    """One report reduced to what counting needs.

    `reactions` carries the rank snapshot taken when the reaction was stored, so a
    later ladder change does not rewrite an old count.
    """

    report_id: int
    author_user_id: int
    created_at: datetime
    reactions: tuple[Reaction, ...] = ()
    ranks: Mapping[int, Rank | None] = field(default_factory=dict)


@dataclass(slots=True)
class VerdictOutcome:
    """The verdict of one report plus why it came out that way."""

    report_id: int
    author_user_id: int
    verdict: str
    by_user_id: int | None = None
    by_ordinal: int | None = None
    conflicted_users: tuple[int, ...] = ()
    ignored: tuple[tuple[int, str], ...] = ()

    @property
    def credited(self) -> bool:
        return self.verdict in CREDITED_VERDICTS


@dataclass(slots=True)
class MemberTally:
    """One person's result for one period."""

    user_id: int
    display_name: str = ""
    tier_label: str | None = None
    tier_ordinal: int | None = None
    approved_count: int = 0
    pending_count: int = 0
    rejected_count: int = 0
    conflict_count: int = 0
    required_count: int = 0

    @property
    def met(self) -> bool:
        """Whether the quota was reached.

        With no quota configured this is False, not True: "0 of 0" must never render as
        a satisfied requirement, or every untracked tier would look like a pass.
        """
        return (
            self.required_count > 0
            and (self.approved_count + self.pending_count) >= self.required_count
        )


@dataclass(slots=True)
class CountSummary:
    """The whole result of counting one period for one binding."""

    tallies: list[MemberTally] = field(default_factory=list)
    outcomes: list[VerdictOutcome] = field(default_factory=list)
    # Reports whose author holds no configured role. They still appear in the ranks so
    # the numbers are complete, but they cannot have a quota.
    unranked: list[MemberTally] = field(default_factory=list)
    # Reports with no reachable author, kept so nothing silently vanishes.
    orphan_reports: int = 0

    def credited_total(self) -> int:
        return sum(1 for o in self.outcomes if o.credited)

    def pending_total(self) -> int:
        return sum(1 for o in self.outcomes if o.verdict == "pending")

    def conflict_total(self) -> int:
        return sum(1 for o in self.outcomes if o.verdict == CONFLICT)

    def everyone(self, *, include_unranked: bool = True) -> list[MemberTally]:
        rows = list(self.tallies)
        if include_unranked:
            rows.extend(self.unranked)
        return sorted(
            rows,
            key=lambda t: (-t.approved_count, -t.pending_count, t.display_name or str(t.user_id)),
        )


def evaluate_reports(reports: Iterable[CountedReport]) -> list[VerdictOutcome]:
    """Compute every report's verdict from its stored reactions and rank snapshots.

    The verdict engine is reused rather than reimplemented: counting must agree with
    what the bot shows in the channel, or staff would maintain two disagreeing answers.
    """
    outcomes: list[VerdictOutcome] = []
    for report in reports:
        result: VerdictResult = compute_verdict(list(report.reactions), dict(report.ranks))
        outcomes.append(
            VerdictOutcome(
                report_id=report.report_id,
                author_user_id=report.author_user_id,
                verdict=result.verdict,
                by_user_id=result.by_user_id,
                by_ordinal=result.by_ordinal,
                conflicted_users=tuple(result.conflicted_users),
                ignored=tuple(result.ignored),
            )
        )
    return outcomes


@dataclass(frozen=True, slots=True)
class RosterEntry:
    """One person expected on the report. Snapshot of role membership."""

    user_id: int
    display_name: str = ""
    tier_label: str | None = None
    tier_ordinal: int | None = None
    required_count: int = 0


def tally(
    outcomes: Sequence[VerdictOutcome],
    roster: Sequence[RosterEntry],
) -> CountSummary:
    """Fold verdicts and a roster into per-person counts.

    Everyone on the roster appears, including those with zero reports: "nobody reported"
    and "nobody is being tracked" must not look the same in the output.

    A report by someone not on the roster is not dropped. It is counted and listed
    separately, because a missing role is an admin mistake and the instructor should
    not lose work over it.
    """
    by_user: dict[int, MemberTally] = {}
    for entry in roster:
        by_user[entry.user_id] = MemberTally(
            user_id=entry.user_id,
            display_name=entry.display_name,
            tier_label=entry.tier_label,
            tier_ordinal=entry.tier_ordinal,
            required_count=entry.required_count,
        )

    summary = CountSummary(outcomes=list(outcomes))
    seen_unranked: dict[int, MemberTally] = {}

    for outcome in outcomes:
        row = by_user.get(outcome.author_user_id)
        if row is None:
            # One row per unranked author, not one per report.
            row = seen_unranked.setdefault(
                outcome.author_user_id, MemberTally(user_id=outcome.author_user_id)
            )

        match outcome.verdict:
            case "approved":
                row.approved_count += 1
            case "pending":
                # Counted, but shown separately so a backlog is visible.
                row.pending_count += 1
            case "rejected":
                row.rejected_count += 1
            case "conflict":
                row.conflict_count += 1

    for entry in roster:
        summary.tallies.append(by_user[entry.user_id])

    summary.unranked = list(seen_unranked.values())
    return summary


__all__ = [
    "CONFLICT",
    "CREDITED_VERDICTS",
    "REJECTED",
    "CountSummary",
    "CountedReport",
    "MemberTally",
    "RosterEntry",
    "VerdictOutcome",
    "evaluate_reports",
    "tally",
]
