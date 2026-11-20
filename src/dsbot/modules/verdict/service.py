"""Reactions -> stored verdicts, and telling staff when something is ambiguous.

The report's verdict is a function of its current reactions and the ranks captured when
those reactions were stored. Every reaction event recomputes it; nothing is applied
incrementally. Removing the deciding reaction therefore reverts to the next surviving
verdict for free, which is the behaviour staff already expect from an undo.

A conflicting verdict (two reviewers of equal authority disagreeing) is not guessed at.
The report is marked `conflict` and staff are told once, when the conflict appears, not
on every subsequent recomputation.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

from dsbot.modules.ranks.resolver import RankResolver
from dsbot.modules.reports.templates import DEFAULT_LOCALE, VERDICT_CONFLICT
from dsbot.modules.verdict.engine import Rank, Reaction, compute_verdict
from dsbot.storage import repos
from dsbot.storage.db import Database

if TYPE_CHECKING:
    from dsbot.discord.reply import ReplySink

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
        reply: ReplySink | None = None,
    ) -> None:
        self._db = db
        self._resolver = resolver
        self._reply = reply
        # Reports whose conflict has already been announced. Bounded: a long-running bot
        # would otherwise remember every conflicted report forever, and the oldest
        # entries are the least likely to matter again.
        self._announced: OrderedDict[tuple[int, str], None] = OrderedDict()
        self._announced_limit = 500

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
        key_marker = (report_id, "conflict")

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
            await self._announce_conflict(update)
        elif update.changed:
            # The conflict was resolved (or replaced by another verdict). Forget it, so a
            # fresh disagreement on the same report is announced again instead of being
            # swallowed by the earlier notice.
            self._announced.pop(key_marker, None)
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

    async def _announce_conflict(self, update: VerdictUpdate) -> None:
        key = (update.report_id, "conflict")
        if key in self._announced:
            return
        self._announced[key] = None
        while len(self._announced) > self._announced_limit:
            self._announced.popitem(last=False)
        if self._reply is None:
            log.warning(
                "verdict_conflict_unannounced",
                report_id=update.report_id,
                users=update.conflicted_users,
                hint="no reply sink configured",
            )
            return

        users = ", ".join(f"<@{uid}>" for uid in update.conflicted_users) or "-"
        template = VERDICT_CONFLICT.get(DEFAULT_LOCALE, "")
        link = f"https://discord.com/channels/{update.channel_id}/{update.message_id}"
        body = template.format(link=link, users=users)
        posted = await self._reply.send(update.channel_id, body)
        if posted is None:
            log.error(
                "verdict_conflict_not_posted",
                report_id=update.report_id,
                channel_id=update.channel_id,
            )
            # Let the next recomputation try again rather than silently never retrying.
            self._announced.pop(key, None)
            return
        log.info(
            "verdict_conflict_announced",
            report_id=update.report_id,
            channel_id=update.channel_id,
            message_id=posted,
        )


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
