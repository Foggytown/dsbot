"""Permission requirements.

The important assertion here is a negative one: MANAGE_MESSAGES must NOT be required,
because a bot may delete its own messages without it. Discord's docs for Delete Message:
"If operating on a guild channel and trying to delete a message that was not sent by the
current user, this endpoint requires the MANAGE_MESSAGES permission."

Requiring it anyway would be an unnecessary privilege escalation.
"""

from __future__ import annotations

import discord

from dsbot.discord.preflight import REQUIRED_CHANNEL_PERMS, invite_url


def test_manage_messages_is_not_required() -> None:
    attrs = {attr for attr, _label in REQUIRED_CHANNEL_PERMS}
    assert "manage_messages" not in attrs


def test_required_permissions_are_minimal() -> None:
    attrs = {attr for attr, _label in REQUIRED_CHANNEL_PERMS}
    assert attrs == {
        "view_channel",
        "read_message_history",
        "send_messages",
        "add_reactions",
    }


def test_every_permission_has_a_human_label() -> None:
    for _attr, label in REQUIRED_CHANNEL_PERMS:
        assert label and label[0].isupper()


def test_invite_url_requests_no_manage_messages() -> None:
    class _FakeUser:
        id = 1234567890

    class _FakeClient:
        user = _FakeUser()

    url = invite_url(_FakeClient())  # type: ignore[arg-type]
    assert "client_id=1234567890" in url
    assert "scope=bot" in url

    value = int(url.split("permissions=")[1].split("&")[0])
    perms = discord.Permissions(value)
    assert perms.send_messages
    assert perms.read_message_history
    assert perms.view_channel
    assert not perms.manage_messages
    assert not perms.administrator
