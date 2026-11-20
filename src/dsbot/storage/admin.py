"""Admin configuration operations.

These are the functions the Discord admin commands will eventually call. Kept
separate from the CLI and from Discord so both can reuse them, and so configuration
can be tested without a gateway connection.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dsbot.modules.reports.schema import FormSchemaDef
from dsbot.storage.models import (
    BindingSchema,
    Channel,
    ChannelBinding,
    FormSchema,
    Guild,
)

_DEFAULT_LADDER_KEY = "default"


async def ensure_schema_seed(
    session: AsyncSession, schemas: Sequence[FormSchemaDef]
) -> dict[str, int]:
    """Insert or update built-in schemas. Returns key -> id."""
    ids: dict[str, int] = {}
    for definition in schemas:
        row = await session.scalar(select(FormSchema).where(FormSchema.key == definition.key))
        if row is None:
            row = FormSchema(
                key=definition.key,
                title=definition.title,
                locale=definition.locale,
                definition=definition.to_definition(),
            )
            session.add(row)
            await session.flush()
        else:
            row.title = definition.title
            row.definition = definition.to_definition()
            row.version += 1
        ids[definition.key] = row.id
    return ids


async def ensure_guild_and_channel(
    session: AsyncSession,
    *,
    guild_id: int,
    channel_id: int,
    guild_name: str = "",
    channel_name: str = "",
) -> Channel:
    guild = await session.get(Guild, guild_id)
    if guild is None:
        session.add(Guild(id=guild_id, name=guild_name, is_report_guild=True))
        await session.flush()
    else:
        guild.is_report_guild = True
        if guild_name:
            guild.name = guild_name

    channel = await session.get(Channel, channel_id)
    if channel is None:
        channel = Channel(id=channel_id, guild_id=guild_id, name=channel_name)
        session.add(channel)
        await session.flush()
    elif channel_name and channel.name != channel_name:
        channel.name = channel_name
    return channel


async def list_all_bindings(session: AsyncSession) -> Sequence[ChannelBinding]:
    """Every binding, enabled or not, for management listings."""
    result = await session.scalars(select(ChannelBinding).order_by(ChannelBinding.id))
    return result.all()


async def set_binding_enabled(session: AsyncSession, channel_id: int, enabled: bool) -> bool:
    """Turn a channel's binding on or off. Returns False if the channel is not bound.

    Disabling is the right way to pause a channel: the binding, its form, and all stored
    history are kept, so re-enabling restores everything with one command.
    """
    binding = await session.scalar(
        select(ChannelBinding).where(ChannelBinding.channel_id == channel_id)
    )
    if binding is None:
        return False
    binding.enabled = enabled
    return True


async def refresh_guild_names(session: AsyncSession, names: dict[int, str]) -> int:
    """Store real guild names, so `dsbot channels` is readable instead of showing '?'.

    Called by commands that have a live Discord connection. Keeps the DB honest without
    requiring the operator to type names.
    """
    updated = 0
    for guild_id, name in names.items():
        if not name:
            continue
        guild = await session.get(Guild, guild_id)
        if guild is None:
            session.add(Guild(id=guild_id, name=name))
            updated += 1
        elif guild.name != name:
            guild.name = name
            updated += 1
    return updated


async def bind_channel(
    session: AsyncSession,
    *,
    guild_id: int,
    channel_id: int,
    module_key: str,
    schema_key: str | None = None,
    rank_source_guild_id: int | None = None,
    locale: str = "ru",
) -> ChannelBinding:
    """Create or update the binding for a channel, plus its schema priority list."""
    await ensure_guild_and_channel(session, guild_id=guild_id, channel_id=channel_id)

    binding = await session.scalar(
        select(ChannelBinding).where(ChannelBinding.channel_id == channel_id)
    )
    if binding is None:
        binding = ChannelBinding(
            channel_id=channel_id,
            guild_id=guild_id,
            module_key=module_key,
            rank_source_guild_id=rank_source_guild_id,
            locale=locale,
            config={},
            enabled=True,
        )
        session.add(binding)
        await session.flush()
    else:
        binding.module_key = module_key
        binding.locale = locale
        # Do not silently re-enable a channel an admin paused: applying a form to a
        # paused channel should update the form, not resume it.
        if rank_source_guild_id is not None:
            binding.rank_source_guild_id = rank_source_guild_id

    if schema_key is not None:
        schema = await session.scalar(select(FormSchema).where(FormSchema.key == schema_key))
        if schema is None:
            raise ValueError(f"unknown schema key: {schema_key}")
        existing = await session.scalar(
            select(BindingSchema).where(
                BindingSchema.binding_id == binding.id,
                BindingSchema.schema_id == schema.id,
            )
        )
        if existing is None:
            session.add(BindingSchema(binding_id=binding.id, schema_id=schema.id, priority=100))

    if rank_source_guild_id is not None:
        await ensure_rank_source_guild(session, rank_source_guild_id)

    return binding


async def ensure_rank_source_guild(session: AsyncSession, guild_id: int) -> None:
    guild = await session.get(Guild, guild_id)
    if guild is None:
        session.add(Guild(id=guild_id, name="", is_rank_source=True))
        return
    guild.is_rank_source = True


async def set_quota(
    session: AsyncSession,
    *,
    ladder_id: int,
    tier_id: int | None,
    binding_id: int | None,
    required_count: int,
    count_rejected: bool = False,
    count_pending: bool = False,
) -> None:
    from dsbot.storage.models import QuotaPolicy

    policy = await session.scalar(
        select(QuotaPolicy).where(
            QuotaPolicy.ladder_id == ladder_id,
            QuotaPolicy.tier_id == tier_id,
            QuotaPolicy.binding_id == binding_id,
        )
    )
    if policy is None:
        session.add(
            QuotaPolicy(
                ladder_id=ladder_id,
                tier_id=tier_id,
                binding_id=binding_id,
                required_count=required_count,
                count_rejected=count_rejected,
                count_pending=count_pending,
            )
        )
        return
    policy.required_count = required_count
    policy.count_rejected = count_rejected
    policy.count_pending = count_pending
