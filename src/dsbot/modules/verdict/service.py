"""Reactions -> stored verdicts.

The report's verdict is a function of its current reactions and the ranks captured when
those reactions were stored. Every reaction event recomputes it; nothing is applied
incrementally. Removing the deciding reaction therefore reverts to the next surviving
verdict for free, which is the behaviour staff already expect from an undo.

A conflicting verdict (two reviewers of equal authority disagreeing) is not guessed at.
The report is marked `conflict`, which counts as declined, and **nothing is announced** -
not once, not ever. That was decided explicitly: an automatic weekly run has to finish
unattended, so no report decision may require a human to read the channel.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import structlog

from dsbot.modules.ranks.resolver import RankResolver
from dsbot.modules.verdict.engine import Rank, Reaction, compute_verdict
from dsbot.storage import repos
from dsbot.storage.db import Database

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class VerdictUpdate:
    """What changed for one report, for logging and for the caller's decisions."""

    report_id: int
    channel_id: int
    message_id: int
    verdict: str
    previous: str | None = None
    decided_by_user_id: int | None = None
    conflicted_users: tuple[int, ...] = ()
    changed: bool = False


class VerdictService:
    def __init__(
        self,
        db: Database,
        resolver: RankResolver | None = None,
    ) -> None:
        self._db = db
        self._resolver = resolver

    async def on_reaction(self, message_id: int) -> VerdictUpdate | None:
        """Recompute the verdict of the report on `message_id`, if it is a report."""
        async with self._db.session() as session:
            report = await repos.get_report_by_message_id(session, message_id)
            if report is None:
                return None
            reactions = await repos.list_reactions(session, message_id)
            report_id = report.id
            channel_id = report.channel_id
            previous = report.verdict
            author_id = report.author_user_id

        snapshot = _snapshot(reactions)
        result = compute_verdict(snapshot.reactions, snapshot.ranks)
        degraded = snapshot.unknown_reviewers > 0

        decided_at = datetime.now(UTC) if result.verdict != "pending" else None
        async with self._db.write_session() as session:
            await repos.put_report_verdict(
                session,
                report_id=report_id,
                verdict=result.verdict,
                decided_by_user_id=result.by_user_id,
                tier_ordinal=result.by_ordinal,
                decided_at=decided_at,
                previous_verdict=previous,
            )

        update = VerdictUpdate(
            report_id=report_id,
            channel_id=channel_id,
            message_id=message_id,
            verdict=result.verdict,
            previous=previous,
            decided_by_user_id=result.by_user_id,
            conflicted_users=tuple(result.conflicted_users),
            changed=previous != result.verdict,
        )
        if degraded:
            # Counting depends on knowing who may review, so say it out loud instead of
            # quietly producing a smaller number.
            log.warning(
                "verdict_computed_without_ranks",
                report_id=report_id,
                author_id=author_id,
                reactions=len(snapshot.reactions),
                unknown_reviewers=snapshot.unknown_reviewers,
                verdict=result.verdict,
            )

        if update.changed:
            log.info(
                "verdict_changed",
                report_id=report_id,
                previous=previous,
                verdict=result.verdict,
                by_user_id=result.by_user_id,
                tier_ordinal=result.by_ordinal,
            )

        if result.verdict == "conflict":
            # Logged, never posted. A same-tier disagreement is a staff problem, and the
            # report channel is not the place to debate it: an automatic weekly run must
            # be able to finish without producing noise, and the count already records the
            # report as not credited. Silence here is deliberate.
            log.warning(
                "verdict_conflict",
                report_id=report_id,
                channel_id=channel_id,
                users=list(result.conflicted_users),
                hint="same-tier reviewers disagree; the report does not count",
            )
        return update

    async def resolve_ranks(
        self, guild_id: int, reactions: list[Reaction]
    ) -> dict[int, Rank | None]:
        """Resolve every reactor's rank in the rank-source guild.

        Kept separate from `on_reaction` so a test, or a replay, can supply ranks
        without a Discord connection.
        """
        ranks: dict[int, Rank | None] = {}
        if self._resolver is None:
            return ranks
        for user_id in {r.user_id for r in reactions}:
            resolved = await self._resolver.resolve(guild_id, user_id)
            if resolved is None:
                ranks[user_id] = None
                continue
            ranks[user_id] = Rank(
                user_id=user_id,
                ordinal=resolved.ordinal or 0,
                tier_label=resolved.tier_label,
            )
        return ranks


@dataclass(frozen=True, slots=True)
class ReactionSnapshot:
    reactions: list[Reaction]
    ranks: dict[int, Rank | None]
    unknown_reviewers: int


def _snapshot(reactions: object) -> ReactionSnapshot:
    """Convert stored reaction rows into the engine's inputs.

    `can_review` is NULL when the rank could not be resolved when the reaction was
    stored (a backfill, or Discord being unreachable). Those are treated as **not**
    reviewers, never as reviewers: a NULL must not be able to grant approval, because
    that would let any stranger's reaction approve a report. The report then stays
    undecided, and undecided still counts for its author, so nothing is lost.
    """
    from dsbot.storage.models import MessageReaction

    typed = [r for r in reactions if isinstance(r, MessageReaction)]  # type: ignore[union-attr]
    engine_reactions: list[Reaction] = []
    ranks: dict[int, Rank | None] = {}
    unknown = 0

    for row in typed:
        emoji = row.emoji_name
        if emoji is None and row.emoji_key.startswith("unicode:"):
            emoji = row.emoji_key.split(":", 1)[1]
        engine_reactions.append(
            Reaction(user_id=row.user_id, emoji_name=emoji, emoji_id=row.emoji_id)
        )
        if row.can_review is True:
            ranks[row.user_id] = Rank(
                user_id=row.user_id,
                ordinal=row.tier_ordinal or 0,
                tier_label=row.tier_label,
            )
        else:
            if row.can_review is None:
                unknown += 1
            ranks[row.user_id] = None

    return ReactionSnapshot(reactions=engine_reactions, ranks=ranks, unknown_reviewers=unknown)


__all__ = ["VerdictService", "VerdictUpdate"]
