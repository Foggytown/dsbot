"""SQL for rank ladders. Everything that touches `rank_ladders` lives here."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import structlog
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from dsbot.modules.ranks.ladder import LadderSpec
from dsbot.modules.ranks.resolver import GuildLadders, LadderIndex
from dsbot.storage.models import RankLadder, RankTier, RankTierRole, TierCapability

log = structlog.get_logger(__name__)


async def replace_ladder(session: AsyncSession, spec: LadderSpec) -> RankLadder:
    """Bring one ladder in line with `spec`, **updating it in place**.

    Content is matched by tier `key`, never by rebuilding the rows. That is the whole
    point: `roster_members`, `quota_policies`, `weekly_results` and `channel_bindings` all
    reference `rank_ladders.id` and `rank_tiers.id`, and three of those cascade on delete.
    Rebuilding the ladder therefore destroyed things an admin had configured by hand:

      * every `quota_policies` override for the ladder vanished (`ON DELETE CASCADE`), so
        one edited number in `quota.yaml` silently discarded every `/dsbot quota-set`;
      * the whole roster vanished, so the weekly report claimed nobody was tracked.

    Only the ladder with the same key in the same guild is touched; other ladders in that
    guild are untouched, because a guild may need both a review ladder and quota ladders.

    Identical content is not even rewritten, so a redeploy costs one comparison.
    """
    existing = await session.scalar(
        select(RankLadder).where(RankLadder.guild_id == spec.guild_id, RankLadder.key == spec.key)
    )

    if existing is None:
        return await _create_ladder(session, spec)

    existing.name = spec.name
    existing.purpose = spec.purpose
    await session.flush()

    if await _ladder_matches(session, existing, spec):
        return existing

    await _sync_tiers(session, existing, spec)
    return existing


async def _create_ladder(session: AsyncSession, spec: LadderSpec) -> RankLadder:
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


async def _sync_tiers(session: AsyncSession, ladder: RankLadder, spec: LadderSpec) -> None:
    """Make the ladder's tiers match `spec`, keeping each surviving tier's id.

    Three things this has to get right:

    1. **A removed tier may still be referenced.** `quota_policies.tier_id` has no
       `ON DELETE` rule, so deleting a tier that a channel overrides would raise a
       foreign-key error. The reference is cleared first: the override was about a tier
       that no longer exists, so keeping it would be worse than dropping it.
    2. **`UNIQUE(ladder_id, ordinal)` forbids swapping two tiers in one step.** Moving
       A 10->20 while B 20->10 collides unless one of them moves out of the way first, so
       every ticket is parked on a unique negative value before the real ordinals are
       applied.
    3. **Roles and capabilities are replaced wholesale** when they differ. They carry no
       references, so rebuilding those rows is safe.
    """
    from dsbot.storage.models import QuotaPolicy

    stored = {
        tier.key: tier
        for tier in (
            await session.scalars(select(RankTier).where(RankTier.ladder_id == ladder.id))
        ).all()
    }
    wanted = {tier_spec.key: tier_spec for tier_spec in spec.tiers}

    # 1. Remove tiers that are gone from the file, releasing any override pointing at them.
    for key, tier in stored.items():
        if key in wanted:
            continue
        await session.execute(
            update(QuotaPolicy).where(QuotaPolicy.tier_id == tier.id).values(tier_id=None)
        )
        await session.delete(tier)
    await session.flush()

    # 2. Park every surviving ordinal on a unique negative value, so a swap cannot collide.
    survivors = {key: tier for key, tier in stored.items() if key in wanted}
    if survivors:
        for tier in survivors.values():
            tier.ordinal = -tier.id
        await session.flush()

    # 3. Apply the desired content.
    for key, tier_spec in wanted.items():
        label = tier_spec.label or tier_spec.key
        tier = survivors.get(key)
        if tier is None:
            tier = RankTier(
                ladder_id=ladder.id,
                key=key,
                label=label,
                ordinal=tier_spec.ordinal,
                required_per_week=tier_spec.required_per_week,
            )
            session.add(tier)
            await session.flush()
            survivors[key] = tier
        else:
            tier.label = label
            tier.ordinal = tier_spec.ordinal
            tier.required_per_week = tier_spec.required_per_week

        await _sync_tier_roles(session, tier, tier_spec)
    await session.flush()

    log.info(
        "ladder_synced_in_place",
        ladder_id=ladder.id,
        key=ladder.key,
        tiers=sorted(wanted),
        removed=sorted(set(stored) - set(wanted)),
    )


async def _sync_tier_roles(session: AsyncSession, tier: RankTier, spec: Any) -> None:
    """Rewrite one tier's roles and capabilities, but only when they differ."""
    current_roles = set(
        (
            await session.scalars(
                select(RankTierRole.role_id).where(RankTierRole.tier_id == tier.id)
            )
        ).all()
    )
    wanted_roles = set(spec.roles)
    if current_roles != wanted_roles:
        await session.execute(delete(RankTierRole).where(RankTierRole.tier_id == tier.id))
        for role_id in wanted_roles:
            session.add(RankTierRole(tier_id=tier.id, role_id=role_id))

    current_caps = set(
        (
            await session.scalars(
                select(TierCapability.capability).where(TierCapability.tier_id == tier.id)
            )
        ).all()
    )
    wanted_caps = set(spec.capabilities)
    if current_caps != wanted_caps:
        await session.execute(delete(TierCapability).where(TierCapability.tier_id == tier.id))
        for capability in wanted_caps:
            session.add(TierCapability(tier_id=tier.id, capability=capability))


async def _ladder_matches(session: AsyncSession, ladder: RankLadder, spec: LadderSpec) -> bool:
    """Whether the stored ladder already says exactly what `spec` says."""
    if ladder.purpose != spec.purpose:
        return False
    tiers = (await session.scalars(select(RankTier).where(RankTier.ladder_id == ladder.id))).all()
    stored: dict[str, dict[str, Any]] = {}
    for tier in tiers:
        roles = set(
            (
                await session.scalars(
                    select(RankTierRole.role_id).where(RankTierRole.tier_id == tier.id)
                )
            ).all()
        )
        caps = set(
            (
                await session.scalars(
                    select(TierCapability.capability).where(TierCapability.tier_id == tier.id)
                )
            ).all()
        )
        stored[tier.key] = {
            "label": tier.label,
            "ordinal": tier.ordinal,
            "required": tier.required_per_week,
            "roles": roles,
            "caps": caps,
        }
    if set(stored) != {tier.key for tier in spec.tiers}:
        return False
    for tier_spec in spec.tiers:
        row = stored[tier_spec.key]
        if row["label"] != (tier_spec.label or tier_spec.key):
            return False
        if row["ordinal"] != tier_spec.ordinal:
            return False
        if row["required"] != tier_spec.required_per_week:
            return False
        if row["roles"] != set(tier_spec.roles):
            return False
        if row["caps"] != set(tier_spec.capabilities):
            return False
    return True


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
