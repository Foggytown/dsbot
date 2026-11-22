"""Publishing to a channel the bot cannot write to must say so, never fail silently.

Reproduces the production log:

    admin_report_target  publish_to=<TextChannel #___hidden___>
    publish_send_failed  403 Forbidden (error code: 50001): Missing Access

The setting saved, the count succeeded, and the publication vanished with only a
traceback in the log. From Discord that is indistinguishable from the feature not working.
"""

from __future__ import annotations

from typing import Any

import discord

from dsbot.discord.reply import can_post_to


class _ForbiddenChannel:
    """A channel that rejects sends the way Discord does without access."""

    id = 1234

    def __init__(self) -> None:
        self.attempts = 0

    async def send(self, *args: Any, **kwargs: Any) -> Any:
        self.attempts += 1
        raise discord.Forbidden(_fake_response(403), {"code": 50001, "message": "Missing Access"})


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status
        self.reason = "Forbidden"


def _fake_response(status: int) -> Any:
    """discord.Forbidden wants a real HTTP response object; the type is not exposed."""
    return _FakeResponse(status)


class _WritableChannel:
    id = 5678

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.deleted: list[int] = []

    async def send(self, content: str, **kwargs: Any) -> Any:
        self.sent.append(content)
        return _Probe(len(self.sent), self)


class _Probe:
    def __init__(self, probe_id: int, channel: _WritableChannel) -> None:
        self.id = probe_id
        self._channel = channel

    async def delete(self) -> None:
        self._channel.deleted.append(self.id)


async def test_a_forbidden_channel_reports_what_is_wrong() -> None:
    channel = _ForbiddenChannel()

    problem = await can_post_to(channel)

    assert problem, "a forbidden channel must produce an explanation"
    assert "прав" in problem or "нет" in problem
    assert channel.attempts == 1


async def test_a_writable_channel_passes_and_leaves_nothing_behind() -> None:
    channel = _WritableChannel()

    problem = await can_post_to(channel)

    assert problem == ""
    assert len(channel.sent) == 1
    assert channel.deleted == [1], "the probe message must delete itself"


async def test_the_probe_mentions_no_people() -> None:
    """A probe must not ping anyone, and must not look like a report."""
    channel = _WritableChannel()

    await can_post_to(channel)

    assert "@" not in channel.sent[0]


async def test_a_channel_that_fails_to_delete_still_counts_as_working() -> None:
    """Permission to send is the question; a leftover probe must not fail the check."""

    class _StickyProbe(_Probe):
        async def delete(self) -> None:
            raise discord.Forbidden(_fake_response(403), {"code": 50001})

    class _Channel(_WritableChannel):
        async def send(self, content: str, **kwargs: Any) -> Any:
            self.sent.append(content)
            return _StickyProbe(1, self)

    problem = await can_post_to(_Channel())

    assert problem == ""
