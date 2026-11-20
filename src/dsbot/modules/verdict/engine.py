"""Verdicts as a pure function over the active reaction set.

This is the single most important design decision in the project:

    verdict = f(active reactions, tier ordering)

NOT "the last event wins". Because it is a function of *state* rather than events, an
approver who removes their reaction automatically reverts the report to the next-highest
surviving verdict. Recording "who acted last" instead would silently corrupt history the
moment someone changed their mind.

Everything here is pure: no database, no Discord, no clock. That is what makes the rules
testable, and it is why this module is small.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

Verdict = Literal["pending", "approved", "rejected", "conflict"]

# Which emoji mean what. Unicode codepoints, so they are stable regardless of the
# client; a custom emoji is matched by id in the store, not here.
APPROVE_EMOJI: frozenset[str] = frozenset(
    {"\u2705", "\u2714", "\u2611"}  # white heavy check, heavy check, ballot box with check
)
REJECT_EMOJI: frozenset[str] = frozenset(
    {"\u274c", "\u2716", "\u26d4"}  # cross mark, heavy multiplication x, no entry
)


@dataclass(frozen=True, slots=True)
class Reaction:
    """One active reaction. Mirrors a row of `message_reactions`."""

    user_id: int
    emoji_name: str | None
    emoji_id: int | None = None

    @property
    def is_approval(self) -> bool:
        return self.emoji_name in APPROVE_EMOJI

    @property
    def is_rejection(self) -> bool:
        return self.emoji_name in REJECT_EMOJI


@dataclass(frozen=True, slots=True)
class Rank:
    """The authority a reactor holds. A snapshot, never re-resolved here."""

    user_id: int
    ordinal: int
    tier_label: str | None = None


@dataclass(slots=True)
class VerdictResult:
    verdict: Verdict
    by_user_id: int | None = None
    by_ordinal: int | None = None
    by_label: str | None = None
    # Everyone whose reaction counted, highest first. Useful for the audit trail and
    # for explaining a conflict.
    counted: list[Rank] = field(default_factory=list)
    # Reactions that were ignored, with why.
    ignored: list[tuple[int, str]] = field(default_factory=list)
    # Set when two reactors of the same top rank disagree.
    conflicted_users: tuple[int, ...] = ()

    def to_payload(self) -> dict[str, object]:
        return {
            "verdict": self.verdict,
            "by_user_id": self.by_user_id,
            "by_ordinal": self.by_ordinal,
            "by_label": self.by_label,
            "counted": [r.user_id for r in self.counted],
            "ignored": [{"user_id": u, "reason": r} for u, r in self.ignored],
            "conflicted_users": list(self.conflicted_users),
        }


def compute_verdict(
    reactions: list[Reaction],
    ranks: Mapping[int, Rank | None],
) -> VerdictResult:
    """Work out the verdict from the current reactions and known ranks.

    Rules:
      * a reaction only counts if its author has a rank that may review
      * the highest ordinal decides
      * two authors at the same top ordinal who disagree produce `conflict`
      * no counting reactions means `pending`
    """
    counted: list[Rank] = []
    ignored: list[tuple[int, str]] = []
    approvals: list[Rank] = []
    rejections: list[Rank] = []

    for reaction in reactions:
        rank = ranks.get(reaction.user_id)
        if rank is None:
            ignored.append((reaction.user_id, "rank_unknown"))
            continue
        if not (reaction.is_approval or reaction.is_rejection):
            ignored.append((reaction.user_id, "emoji_ignored"))
            continue
        counted.append(rank)
        if reaction.is_approval:
            approvals.append(rank)
        else:
            rejections.append(rank)

    if not counted:
        return VerdictResult(verdict="pending", ignored=ignored)

    counted.sort(key=lambda r: r.ordinal, reverse=True)

    top_approval = max((r.ordinal for r in approvals), default=None)
    top_rejection = max((r.ordinal for r in rejections), default=None)

    if top_approval is not None and top_rejection is not None and top_approval == top_rejection:
        # Same authority disagreeing with itself: no automatic answer is safe.
        conflicted = tuple(r.user_id for r in counted if r.ordinal == top_approval)
        return VerdictResult(
            verdict="conflict",
            counted=counted,
            ignored=ignored,
            conflicted_users=conflicted,
        )

    decided_by_approval = top_approval is not None and (
        top_rejection is None or top_approval > top_rejection
    )
    winner_pool = approvals if decided_by_approval else rejections
    winner = max(winner_pool, key=lambda r: r.ordinal)

    return VerdictResult(
        verdict="approved" if decided_by_approval else "rejected",
        by_user_id=winner.user_id,
        by_ordinal=winner.ordinal,
        by_label=winner.tier_label,
        counted=counted,
        ignored=ignored,
    )


def classify_emoji(name: str | None) -> str:
    """For logging and for building the emoji configuration UI."""
    if name in APPROVE_EMOJI:
        return "approve"
    if name in REJECT_EMOJI:
        return "reject"
    return "ignore"
