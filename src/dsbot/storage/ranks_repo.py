"""SQL for rank ladders. Everything that touches `rank_ladders` lives here."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from dsbot.modules.ranks.ladder import LadderSpec
from dsbot.modules.ranks.resolver import GuildLadders, LadderIndex
from dsbot.storage.models import RankLadder, RankTier, RankTierRole, TierCapability


async def replace_ladder(session: AsyncSession, spec: LadderSpec) -> RankLadder:
    """Replace one ladder with `spec`.

    Replacement, not merge: a ladder is a single authority ordering, and a partial
    update would leave stale tiers that still match roles. Only the ladder with the
    same key in the same guild is removed; other ladders in that guild are untouched,
    because a guild may need both a review ladder and quota ladders.
    """
    existing = await session.scalar(
        select(RankLadder).where(RankLadder.guild_id == spec.guild_id, RankLadder.key == spec.key)
    )
    if existing is not None:
        await session.execute(delete(RankLadder).where(RankLadder.id == existing.id))
        await session.flush()

    ladder = RankLadder(guild_id=spec.guild_id, key=spec.key, name=spec.name, purpose=spec.purpose)
    session.add(ladder)
    await session.flush()

    for tier_spec in spec.tiers:
        tier = RankTier(
            ladder_id=ladder.id,
            key=tier_spec.key,
            label=tier_spec.label or tier_spec.key,
            ordinal=tier_spec.ordinal,
            required_per_week=tier_spec.required_per_week,
        )
        session.add(tier)
        await session.flush()
        for role_id in tier_spec.roles:
            session.add(RankTierRole(tier_id=tier.id, role_id=role_id))
        for capability in tier_spec.capabilities:
            session.add(TierCapability(tier_id=tier.id, capability=capability))

    await session.flush()
    return ladder


async def list_ladders(
    session: AsyncSession,
) -> Sequence[tuple[RankLadder, list[dict[str, Any]]]]:
    """Every ladder with its tiers, ready for a CLI table."""
    ladders = (await session.scalars(select(RankLadder).order_by(RankLadder.guild_id))).all()
    result: list[tuple[RankLadder, list[dict[str, Any]]]] = []
    for ladder in ladders:
        tiers = (
            await session.scalars(
                select(RankTier)
                .where(RankTier.ladder_id == ladder.id)
                .order_by(RankTier.ordinal.desc())
            )
        ).all()
        rows: list[dict[str, Any]] = []
        for tier in tiers:
            roles = (
                await session.scalars(
                    select(RankTierRole.role_id).where(RankTierRole.tier_id == tier.id)
                )
            ).all()
            caps = (
                await session.scalars(
                    select(TierCapability.capability).where(TierCapability.tier_id == tier.id)
                )
            ).all()
            rows.append(
                {
                    "key": tier.key,
                    "label": tier.label,
                    "ordinal": tier.ordinal,
                    "roles": list(roles),
                    "capabilities": list(caps),
                    "required_per_week": tier.required_per_week,
                }
            )
        result.append((ladder, rows))
    return result


async def load_all_ladders(session: AsyncSession) -> dict[int, GuildLadders]:
    """Build the guild -> ladder-set map the resolver needs.

    A guild may hold several ladders: one that orders reviewers, and one or more that
    describe who owes reports. Both are indexed the same way, and lookups are filtered
    by capability, so the caller asks "who may review" or "who is on a quota" rather
    than picking a ladder by hand.

    Within one capability lookup, the highest ordinal across all of the guild's ladders
    wins, so a guild-wide review tier cannot be silently beaten by a quotadier.
    """
    ladders = (await session.scalars(select(RankLadder))).all()
    by_guild: dict[int, GuildLadders] = {}
    for ladder in ladders:
        tiers = (
            await session.scalars(select(RankTier).where(RankTier.ladder_id == ladder.id))
        ).all()
        by_role: dict[int, tuple[str, str, int, frozenset[str]]] = {}
        quota_by_role: dict[int, int] = {}
        for tier in tiers:
            roles = (
                await session.scalars(
                    select(RankTierRole.role_id).where(RankTierRole.tier_id == tier.id)
                )
            ).all()
            caps = frozenset(
                (
                    await session.scalars(
                        select(TierCapability.capability).where(TierCapability.tier_id == tier.id)
                    )
                ).all()
            )
            for role_id in roles:
                by_role[role_id] = (tier.key, tier.label or tier.key, tier.ordinal, caps)
                if tier.required_per_week is not None:
                    quota_by_role[role_id] = tier.required_per_week
        group = by_guild.setdefault(ladder.guild_id, GuildLadders(guild_id=ladder.guild_id))
        group.add(
            LadderIndex(
                ladder_id=ladder.id,
                guild_id=ladder.guild_id,
                key=ladder.key,
                by_role=by_role,
                purpose=ladder.purpose,
                quota_by_role=quota_by_role,
            )
        )
    return by_guild


async def ladder_for_guild(session: AsyncSession, guild_id: int) -> RankLadder | None:
    """One ladder for a guild, for callers that predate multi-ladder guilds.

    Prefer the guild's full ladder set (`load_all_ladders`) when the answer depends on
    capabilities. This exists so diagnostics that only need "is anything configured"
    stay simple, and it returns the ladder that carries `review` when there is a choice,
    because that is the one that decides verdicts.
    """
    candidates = (
        await session.scalars(
            select(RankLadder).where(RankLadder.guild_id == guild_id).order_by(RankLadder.id)
        )
    ).all()
    if not candidates:
        return None
    for ladder in candidates:
        has_review = await session.scalar(
            select(TierCapability.id)
            .join(RankTier, RankTier.id == TierCapability.tier_id)
            .where(RankTier.ladder_id == ladder.id, TierCapability.capability == "review")
            .limit(1)
        )
        if has_review is not None:
            return ladder
    return candidates[0]


async def ladder_ids_for_guild(session: AsyncSession, guild_id: int) -> Sequence[int]:
    """Ids of every ladder in a guild. Diagnostics and future admin tooling."""
    return list(
        (await session.scalars(select(RankLadder.id).where(RankLadder.guild_id == guild_id))).all()
    )
