"""Discord-backed role lookup for rank resolution.

Two paths, because they solve different problems:

  * cache path  - the member is already known in that guild, so roles come free
  * REST path   - the member is not cached (Members intent off, or never seen), so the
                  member record is fetched explicitly

The REST path is what makes cross-server approval work without forcing the Members
intent, which is a privileged intent and an extra portal step.
"""

from __future__ import annotations

from typing import Any

import discord
import structlog

log = structlog.get_logger(__name__)


class DiscordRankSource:
    """Reads role ids from a live Discord client."""

    def __init__(self, client: discord.Client) -> None:
        self._client = client
        self.rest_calls = 0

    async def role_ids_for(self, guild_id: int, user_id: int) -> frozenset[int] | None:
        guild = self._client.get_guild(guild_id)
        if guild is None:
            log.debug("rank_guild_not_cached", guild_id=guild_id)
            # Without the guild there is nothing to try; the caller reports unresolved.
            return None

        # 1. Cached member, free when the Members intent is on.
        member = guild.get_member(user_id)
        if member is not None:
            return frozenset(role.id for role in member.roles)

        # 2. Explicit fetch. Works with the Members intent off.
        self.rest_calls += 1
        try:
            fetched = await guild.fetch_member(user_id)
        except discord.NotFound:
            # Genuinely not a member of that guild.
            return None
        except discord.HTTPException:
            log.warning("rank_member_fetch_failed", guild_id=guild_id, user_id=user_id)
            return None

        return frozenset(role.id for role in fetched.roles)


class StaticRankSource:
    """Test double: an in-memory map of user -> role ids."""

    def __init__(self, mapping: dict[tuple[int, int], frozenset[int]]) -> None:
        self._mapping = mapping
        self.calls: list[tuple[int, int]] = []

    async def role_ids_for(self, guild_id: int, user_id: int) -> frozenset[int] | None:
        self.calls.append((guild_id, user_id))
        return self._mapping.get((guild_id, user_id))


def ladder_index_from_specs(specs: list[Any]) -> dict[int, Any]:
    """Build guild -> LadderIndex from loaded ladder specs.

    Imported lazily to avoid a cycle between the resolver and the schema module.
    """
    from dsbot.modules.ranks.resolver import LadderIndex

    by_guild: dict[int, LadderIndex] = {}
    for spec in specs:
        index = LadderIndex(
            ladder_id=getattr(spec, "id", None),
            guild_id=spec.guild_id,
            key=spec.key,
            by_role={
                role_id: (
                    tier.key,
                    tier.label or tier.key,
                    tier.ordinal,
                    frozenset(tier.capabilities),
                )
                for tier in spec.tiers
                for role_id in tier.roles
            },
        )
        by_guild[spec.guild_id] = index
    return by_guild
