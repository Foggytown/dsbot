"""Event bus tests: ordering and failure isolation."""

from __future__ import annotations

from datetime import UTC, datetime

from dsbot.core.bus import EventBus
from dsbot.core.events import MessageCreated, MessageSnapshot


def make_event(channel_id: int, message_id: int) -> MessageCreated:
    return MessageCreated(
        message=MessageSnapshot(
            message_id=message_id,
            channel_id=channel_id,
            guild_id=1,
            author_id=2,
            author_name="author",
            author_display_name=None,
            author_is_bot=False,
            content=f"msg {message_id}",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )


async def test_events_in_one_channel_are_ordered() -> None:
    bus = EventBus()
    seen: list[int] = []

    async def handler(event: MessageCreated) -> None:
        seen.append(event.message.message_id)

    bus.subscribe("message_created", handler)
    for i in range(50):
        await bus.publish(make_event(channel_id=10, message_id=i))
    await bus.flush()
    await bus.aclose()

    assert seen == list(range(50))


async def test_channels_do_not_block_each_other() -> None:
    bus = EventBus()
    seen: list[tuple[int, int]] = []

    async def handler(event: MessageCreated) -> None:
        seen.append((event.message.channel_id, event.message.message_id))

    bus.subscribe("message_created", handler)
    for i in range(20):
        await bus.publish(make_event(channel_id=1, message_id=i))
        await bus.publish(make_event(channel_id=2, message_id=i))
    await bus.flush()
    await bus.aclose()

    channel_one = [m for c, m in seen if c == 1]
    channel_two = [m for c, m in seen if c == 2]
    assert channel_one == list(range(20))
    assert channel_two == list(range(20))


async def test_failing_handler_does_not_stop_the_lane() -> None:
    bus = EventBus()
    seen: list[int] = []
    calls = {"n": 0}

    async def flaky(event: MessageCreated) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom")
        seen.append(event.message.message_id)

    bus.subscribe("message_created", flaky)
    for i in range(5):
        await bus.publish(make_event(channel_id=1, message_id=i))
    await bus.flush()
    await bus.aclose()

    assert seen == [0, 2, 3, 4]
    assert bus.failed == 1


async def test_no_subscribers_is_not_an_error() -> None:
    bus = EventBus()
    await bus.publish(make_event(channel_id=1, message_id=1))
    await bus.flush()
    await bus.aclose()
    assert bus.processed == 0


async def test_publish_after_close_raises() -> None:
    bus = EventBus()
    await bus.aclose()
    try:
        await bus.publish(make_event(channel_id=1, message_id=1))
    except RuntimeError:
        return
    raise AssertionError("expected RuntimeError")


async def test_stats_report_lanes() -> None:
    bus = EventBus()

    async def handler(_event: MessageCreated) -> None:
        return None

    bus.subscribe("message_created", handler)
    await bus.publish(make_event(channel_id=1, message_id=1))
    await bus.publish(make_event(channel_id=2, message_id=2))
    await bus.flush()
    stats = bus.stats()
    assert stats["lanes"] == 2
    assert stats["processed"] == 2
    await bus.aclose()


def test_event_external_ids_are_stable() -> None:
    first = make_event(channel_id=1, message_id=5)
    second = make_event(channel_id=1, message_id=5)
    assert first.external_id == second.external_id
