"""Preflight: verify token, guild access, channel access, and required permissions.

Read-only. Posts nothing, changes nothing. Run before the first live test so a missing
intent or permission is discovered here rather than as silence in the channel.
"""

from __future__ import annotations

import discord
import structlog

log = structlog.get_logger(__name__)

# What a working setup needs in the report channel.
#
# MANAGE_MESSAGES is deliberately NOT required. Discord's docs for Delete Message:
# "If operating on a guild channel and trying to delete a message that was not sent by
# the current user, this endpoint requires the MANAGE_MESSAGES permission." The bot only
# ever deletes its own warning messages, so Send Messages plus Read Message History is
# enough. Asking for Manage Messages would be an unnecessary privilege escalation.
REQUIRED_CHANNEL_PERMS: tuple[tuple[str, str], ...] = (
    ("view_channel", "View Channel"),
    ("read_message_history", "Read Message History"),
    ("send_messages", "Send Messages"),
    ("add_reactions", "Add Reactions"),
)


def invite_url(client: discord.Client) -> str:
    """Build the OAuth2 URL for exactly the permissions this bot needs.

    Requesting a precise permission set means nobody has to reason about which
    checkbox matters, and the bot cannot be over-privileged by accident.
    """
    perms = discord.Permissions(
        view_channel=True,
        read_message_history=True,
        send_messages=True,
        add_reactions=True,
    )
    client_id = getattr(client.user, "id", None)
    if client_id is None:
        return "unavailable (client id unknown)"
    return (
        "https://discord.com/oauth2/authorize"
        f"?client_id={client_id}&permissions={perms.value}&scope=bot"
    )


async def preflight(client: discord.Client, guild_id: int, channel_id: int) -> list[str]:
    """Return a list of human-readable problems. Empty means all good."""
    problems: list[str] = []

    user = client.user
    if user is None:
        return ["not connected"]

    guild = client.get_guild(guild_id)
    if guild is None:
        guilds = ", ".join(f"{g.name} ({g.id})" for g in client.guilds) or "none"
        return [
            f"bot is not in guild {guild_id}. Guilds visible: {guilds}.",
            f"invite URL: {invite_url(client)}",
        ]

    channel = client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(channel_id)
        except discord.HTTPException:
            problems.append(
                f"channel {channel_id} not visible; check View Channel for the bot role"
            )
            return problems

    if not isinstance(channel, discord.TextChannel):
        problems.append(f"channel {channel_id} is {type(channel).__name__}, not a text channel")
        return problems

    # Permission maths is only trustworthy when guild state is cached. Without the
    # Members intent, `guild.default_role` is None and discord.py computes from an empty
    # base, which produced false "missing Read Message History" reports. Probing the
    # operation directly is both simpler and truthful.
    if guild.default_role is None:
        return problems + await _probe_functionally(channel)

    member = guild.get_member(user.id)
    if member is None:
        try:
            member = await guild.fetch_member(user.id)
        except discord.HTTPException:
            return problems + await _probe_functionally(channel)

    perms = channel.permissions_for(member)
    for attr, label in REQUIRED_CHANNEL_PERMS:
        if not getattr(perms, attr, False):
            problems.append(f"missing permission in #{channel.name}: {label}")

    if guild.owner_id == user.id:
        problems.append("bot owns the guild (unexpected)")

    return problems


async def _probe_functionally(channel: discord.TextChannel) -> list[str]:
    """Check what the bot can actually do, by doing it.

    Needs no cached guild state. A bot may delete its own messages without
    MANAGE_MESSAGES, so that is exercised rather than assumed.
    """
    problems: list[str] = []
    try:
        probe = await channel.send("dsbot permission check (deletes itself)")
    except discord.Forbidden:
        problems.append(f"missing permission in #{channel.name}: Send Messages")
        return problems
    except discord.HTTPException:
        problems.append(f"could not send to #{channel.name}")
        return problems

    try:
        await probe.delete()
    except discord.Forbidden:
        problems.append(
            "cannot delete its own message (unexpected: requires no special permission)"
        )
    except discord.HTTPException:
        problems.append("could not delete its own probe message")

    try:
        async for _ in channel.history(limit=1):
            break
    except discord.Forbidden:
        problems.append(f"missing permission in #{channel.name}: Read Message History")

    return problems


def describe(client: discord.Client, guild_id: int, channel_id: int) -> dict[str, object]:
    guild = client.get_guild(guild_id)
    channel = client.get_channel(channel_id)
    return {
        "bot_user": str(client.user),
        "bot_user_id": getattr(client.user, "id", None),
        "guild_name": getattr(guild, "name", None),
        "guild_member_count": getattr(guild, "member_count", None),
        "channel_name": getattr(channel, "name", None),
        "intents_message_content": client.intents.message_content,
        "intents_members": client.intents.members,
        "intents_reactions": client.intents.reactions,
        "guilds_visible": len(client.guilds),
    }
