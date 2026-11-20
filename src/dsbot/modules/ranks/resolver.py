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
from dataclasses import dataclass
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

    def resolve_roles(
        self, role_ids: frozenset[int], user_id: int, source: str
    ) -> ResolvedRank | None:
        """Highest tier the user holds. Highest ordinal wins."""
        best: tuple[str, str, int, frozenset[str]] | None = None
        for role_id in role_ids:
            entry = self.by_role.get(role_id)
            if entry is None:
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


class RankResolver:
    """Resolve users to tiers, with an in-process cache."""

    def __init__(
        self,
        source: RankSource,
        ladders: dict[int, LadderIndex],
        *,
        ttl_seconds: float = CACHE_TTL_SECONDS,
    ) -> None:
        self._source = source
        self._ladders = ladders
        self._ttl = ttl_seconds
        # (guild_id, user_id) -> (expires_at, rank or None)
        self._cache: dict[tuple[int, int], tuple[float, ResolvedRank | None]] = {}
        self.hits = 0
        self.misses = 0
        self.unresolved = 0

    def set_ladders(self, ladders: dict[int, LadderIndex]) -> None:
        """Replace the ladders and drop the cache, after a config change."""
        self._ladders = ladders
        self._cache.clear()

    async def resolve(self, guild_id: int, user_id: int) -> ResolvedRank | None:
        """Rank of `user_id` in `guild_id`, or None if they hold no configured role."""
        now = time.monotonic()
        cached = self._cache.get((guild_id, user_id))
        if cached is not None and cached[0] > now:
            self.hits += 1
            return cached[1]

        self.misses += 1
        index = self._ladders.get(guild_id)
        if index is None:
            # No ladder configured for this guild: not an error, just nothing to say.
            self._cache[(guild_id, user_id)] = (now + self._ttl, None)
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
            self._cache[(guild_id, user_id)] = (now + self._ttl, None)
            return None

        rank = index.resolve_roles(role_ids, user_id, source="member")
        if rank is None:
            log.info(
                "rank_unresolved",
                user_id=user_id,
                guild_id=guild_id,
                reason="no_configured_role",
                role_count=len(role_ids),
            )
            self.unresolved += 1

        self._cache[(guild_id, user_id)] = (now + self._ttl, rank)
        return rank

    def invalidate(self, guild_id: int, user_id: int) -> None:
        """Drop one user's cached rank, e.g. after a member update event."""
        self._cache.pop((guild_id, user_id), None)

    def stats(self) -> dict[str, int]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "unresolved": self.unresolved,
            "cached": len(self._cache),
        }
