"""Counting from live Discord history instead of the bot's archive.

The archive only contains what the bot was running for. A weekly report over a period
before the bot started would otherwise return nothing, which looks exactly like "nobody
reported". These tests pin the grouping rules that decide whether a report is complete.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from dsbot.modules.quotas.live import group_messages

START = datetime(2026, 11, 16, 10, 0, tzinfo=UTC)


class User:
    def __init__(self, uid: int, name: str = "user", bot: bool = False) -> None:
        self.id = uid
        self.name = name
        self.display_name = name
        self.bot = bot


class Message:
    def __init__(
        self,
        mid: int,
        author: User,
        content: str,
        minutes: float,
        *,
        attachments: int = 0,
    ) -> None:
        self.id = mid
        self.author = author
        self.content = content
        self.created_at = START + timedelta(minutes=minutes)
        self.attachments = [object()] * attachments


ANN = User(1, "Ann")
BOB = User(2, "Bob")


def test_a_photo_follow_up_joins_its_report() -> None:
    groups = group_messages(
        [
            Message(1, ANN, "1. ARC | 0001\n2. T\n3.", 0),
            Message(2, ANN, "", 1, attachments=1),
        ]
    )
    assert len(groups) == 1
    assert len(groups[0]) == 2


def test_a_follow_up_by_another_author_does_not_join() -> None:
    """Otherwise one person's photo completes someone else's report."""
    groups = group_messages(
        [
            Message(1, ANN, "1. ARC | 0001\n2. T\n3.", 0),
            Message(2, BOB, "", 1, attachments=1),
        ]
    )
    assert len(groups) == 2


def test_text_after_a_report_starts_a_new_group() -> None:
    """A later message with text is chat or a new report, never evidence."""
    groups = group_messages(
        [
            Message(1, ANN, "1. ARC | 0001\n2. T\n3.", 0),
            Message(2, ANN, "цифра 4", 1),
        ]
    )
    assert len(groups) == 2


def test_a_photo_too_late_does_not_join() -> None:
    """An unbounded window would attach a photo from an hour later to an old report."""
    groups = group_messages(
        [
            Message(1, ANN, "1. ARC | 0001\n2. T\n3.", 0),
            Message(2, ANN, "", 60, attachments=1),
        ]
    )
    assert len(groups) == 2


def test_several_follow_ups_chain_onto_one_report() -> None:
    groups = group_messages(
        [
            Message(1, ANN, "1. ARC | 0001\n2. M-L-C\n3.", 0),
            Message(2, ANN, "", 1, attachments=1),
            Message(3, ANN, "", 2, attachments=1),
        ]
    )
    assert len(groups) == 1
    assert len(groups[0]) == 3


def test_groups_are_ordered_by_time_regardless_of_input_order() -> None:
    """Discord returns newest-first; the group must still read oldest-first."""
    groups = group_messages(
        [
            Message(2, ANN, "", 1, attachments=1),
            Message(1, ANN, "1. ARC | 0001\n2. T\n3.", 0),
        ]
    )
    assert groups[0][0].id == 1
    assert groups[0][1].id == 2


def test_text_with_attachments_does_not_get_absorbed() -> None:
    """The historical bug: a report stole the photos of the next report."""
    groups = group_messages(
        [
            Message(1, ANN, "1. ARC | 0001\n2. T\n3.", 0),
            Message(2, ANN, "1. ARC | 0002\n2. T\n3.", 5, attachments=2),
        ]
    )
    assert len(groups) == 2
    assert len(groups[1]) == 1
