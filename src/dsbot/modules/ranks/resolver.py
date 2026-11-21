"""Resolve a Discord user to a rank tier, across servers.

The problem: approvers live in specialization servers, but reports are read from a
different server. When someone reacts in the report server, discord.py hands us a `User`
with no `.roles` at all, because that user is not a member where the message lives.

So a rank is resolved in three steps, cheapest first:

  1. cached rank, if fresh
  2. member record in the rank-source guild, from cache
  3. REST fetch of that member

Anything unresolved returns None, and the caller records the reaction without a verdict.
Guessing a rank would silently give the wrong person authority.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol

import structlog

log = structlog.get_logger(__name__)

# How long a cached rank is trusted. Roles change rarely, so this can be generous.
CACHE_TTL_SECONDS = 900


@dataclass(frozen=True, slots=True)
class ResolvedRank:
    """A user's authority in one guild."""

    user_id: int
    guild_id: int
    ladder_id: int | None
    tier_id: int | None
    tier_key: str | None
    tier_label: str | None
    ordinal: int | None
    capabilities: frozenset[str]
    source: str  # cache | member | rest | manual

    @property
    def can_review(self) -> bool:
        return "review" in self.capabilities

    @property
    def can_admin(self) -> bool:
        return "admin" in self.capabilities


class RankSource(Protocol):
    """Where role ids come from. Implemented by Discord and by tests."""

    async def role_ids_for(self, guild_id: int, user_id: int) -> frozenset[int] | None:
        """Role ids the user holds in that guild, or None if not a member."""
        ...


@dataclass(slots=True)
class LadderIndex:
    """Tiers of one ladder, indexed by role id for O(1) lookup."""

    ladder_id: int | None
    guild_id: int
    key: str
    # role id -> (tier_key, label, ordinal, capabilities)
    by_role: dict[int, tuple[str, str, int, frozenset[str]]]
    # "review" or "quota", declared by the admin rather than inferred.
    purpose: str = "review"
    # role id -> reports per week, from the quota ladder only.
    quota_by_role: dict[int, int] = field(default_factory=dict)

    def resolve_roles(
        self,
        role_ids: frozenset[int],
        user_id: int,
        source: str,
        *,
        requires: frozenset[str] = frozenset(),
    ) -> ResolvedRank | None:
        """Highest tier the user holds, optionally restricted by capability.

        `requires` is what makes quotas and approvals separable. The same ladder can
        describe both "who may approve" and "who owes reports": the first asks for
        tiers with `review`, the second asks for no capability at all. Without it, a
        person's rank would have to mean one thing, and a reviewer quota would leak into
        a reporting tier.
        """
        best: tuple[str, str, int, frozenset[str]] | None = None
        for role_id in role_ids:
            entry = self.by_role.get(role_id)
            if entry is None:
                continue
            if requires and not requires <= entry[3]:
                continue
            if best is None or entry[2] > best[2]:
                best = entry
        if best is None:
            return None
        tier_key, label, ordinal, capabilities = best
        return ResolvedRank(
            user_id=user_id,
            guild_id=self.guild_id,
            ladder_id=self.ladder_id,
            tier_id=None,
            tier_key=tier_key,
            tier_label=label,
            ordinal=ordinal,
            capabilities=capabilities,
            source=source,
        )


@dataclass(slots=True)
class GuildLadders:
    """Every ladder that applies to one guild.

    A guild commonly has more than one: a ladder that orders who may review, and
    ladders that describe who owes reports. Keeping them together means a lookup can
    ask "who may review here" or "is this person on any quota" without the caller
    having to know which ladder answers which question.
    """

    guild_id: int
    indexes: list[LadderIndex] = field(default_factory=list)

    def add(self, index: LadderIndex) -> None:
        self.indexes.append(index)

    @property
    def by_role(self) -> dict[int, tuple[str, str, int, frozenset[str]]]:
        """All roles from every ladder, later ladders winning conflicts.

        Used for diagnostics ("how many roles are configured") and for a lookup that
        does not care which ladder a role came from.
        """
        merged: dict[int, tuple[str, str, int, frozenset[str]]] = {}
        for index in self.indexes:
            merged.update(index.by_role)
        return merged

    @property
    def quota_ladder_id(self) -> int | None:
        """Which ladder holds the people who owe reports.

        Chosen by declared purpose, not by guessing from capabilities: an admin says
        which ladder is the quota one, and an unfinished review ladder therefore cannot
        be mistaken for it.
        """
        for index in self.indexes:
            if index.purpose == "quota":
                return index.ladder_id
        return None

    def required_for_roles(self, role_ids: frozenset[int]) -> tuple[int, str | None, int | None]:
        """Weekly requirement for someone holding these roles, and which tier set it.

        Highest ordinal wins, so a person holding several quota roles is asked for the
        senior amount, never the sum: a promotion must not double the duty, and a leftover
        junior role must not drag it back down.

        Returns (required, tier_label, tier_ordinal). `required` is 0 when the person is
        on no quota ladder at all.
        """
        best: tuple[int, str | None, int | None] | None = None
        for index in self.indexes:
            if index.purpose != "quota":
                continue
            for role_id in role_ids:
                requirement = index.quota_by_role.get(role_id)
                if requirement is None:
                    continue
                entry = index.by_role.get(role_id)
                ordinal = entry[2] if entry else 0
                label = entry[1] if entry else None
                if best is None or ordinal > (best[2] or 0):
                    best = (requirement, label, ordinal)
        return best if best is not None else (0, None, None)

    def resolve(
        self,
        role_ids: frozenset[int],
        user_id: int,
        source: str,
        *,
        requires: frozenset[str] = frozenset(),
        only_ladder_id: int | None = None,
    ) -> ResolvedRank | None:
        """Highest tier among this guild's ladders, filtered by capability and ladder.

        `only_ladder_id` is how a channel pins its hierarchy. Without it every ladder in
        the guild competes and the highest ordinal wins, which is right for a shared
        ladder but wrong when a channel deliberately points at its own one.
        """
        best: ResolvedRank | None = None
        for index in self.indexes:
            if only_ladder_id is not None and index.ladder_id != only_ladder_id:
                continue
            candidate = index.resolve_roles(role_ids, user_id, source, requires=requires)
            if candidate is None:
                continue
            if best is None or (candidate.ordinal or 0) > (best.ordinal or 0):
                best = candidate
        return best


class RankResolver:
    """Resolve users to tiers, with an in-process cache."""

    def __init__(
        self,
        source: RankSource,
        ladders: dict[int, GuildLadders],
        *,
        ttl_seconds: float = CACHE_TTL_SECONDS,
    ) -> None:
        self._source = source
        self._ladders = ladders
        self._ttl = ttl_seconds
        # (guild_id, user_id, requires) -> (expires_at, rank or None)
        self._cache: dict[
            tuple[int, int, frozenset[str], int | None], tuple[float, ResolvedRank | None]
        ] = {}
        self.hits = 0
        self.misses = 0
        self.unresolved = 0

    def set_ladders(self, ladders: dict[int, GuildLadders]) -> None:
        """Replace the ladders and drop the cache, after a config change."""
        self._ladders = ladders
        self._cache.clear()

    async def resolve(
        self,
        guild_id: int,
        user_id: int,
        *,
        requires: frozenset[str] = frozenset(),
        ladder_id: int | None = None,
    ) -> ResolvedRank | None:
        """Rank of `user_id` in `guild_id`, or None if they hold no matching role.

        The cache key includes `requires` and `ladder_id`, because "who may review",
        "who owes a report", and "who ranks above whom in this channel's ladder" are
        different questions about the same person and must not share an answer.
        """
        now = time.monotonic()
        cache_key = (guild_id, user_id, requires, ladder_id)
        cached = self._cache.get(cache_key)
        if cached is not None and cached[0] > now:
            self.hits += 1
            return cached[1]

        self.misses += 1
        group = self._ladders.get(guild_id)
        if group is None:
            # No ladder configured for this guild: not an error, just nothing to say.
            self._cache[cache_key] = (now + self._ttl, None)
            return None

        role_ids = await self._source.role_ids_for(guild_id, user_id)
        if role_ids is None:
            log.info(
                "rank_unresolved",
                user_id=user_id,
                guild_id=guild_id,
                reason="not_a_member",
            )
            self.unresolved += 1
            self._cache[cache_key] = (now + self._ttl, None)
            return None

        rank = group.resolve(
            role_ids,
            user_id,
            source="member",
            requires=requires,
            only_ladder_id=ladder_id,
        )
        if rank is None:
            log.info(
                "rank_unresolved",
                user_id=user_id,
                guild_id=guild_id,
                reason="no_configured_role",
                role_count=len(role_ids),
                requires=sorted(requires),
            )
            self.unresolved += 1

        self._cache[cache_key] = (now + self._ttl, rank)
        return rank

    def invalidate(self, guild_id: int, user_id: int) -> None:
        """Drop one user's cached rank, e.g. after a member update event."""
        for key in [k for k in self._cache if k[0] == guild_id and k[1] == user_id]:
            self._cache.pop(key, None)

    def stats(self) -> dict[str, int]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "unresolved": self.unresolved,
            "cached": len(self._cache),
        }
