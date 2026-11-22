"""A link in the wrong numbered field must be named, not swallowed.

The quiet failure this covers: a reporter pastes the previous report's Discord link into
field 3 instead of field 4. Field 3 is the photos field and accepts text, so the link is
stored without complaint, the promotion link is simply absent, and - if photos were also
attached - the report was judged **valid**.
"""

from __future__ import annotations

from datetime import UTC, datetime

from dsbot.modules.reports.parser import GroupMessage, ReportCandidate, parse_report
from dsbot.modules.reports.schema import DEFAULT_SCHEMAS

LINK = "https://discord.com/channels/1274402776696488108/1339645935348613191/1348671185625415771"


def parse(text: str, *, attachments: int = 2) -> object:
    candidate = ReportCandidate(
        messages=[
            GroupMessage(
                message_id=1,
                author_id=1,
                created_at=datetime.now(UTC),
                content=text,
                attachment_count=attachments,
            )
        ]
    )
    return parse_report(list(DEFAULT_SCHEMAS), candidate, attachment_count=attachments)


def codes(result: object) -> list[str]:
    return [issue.code for issue in result.issues]  # type: ignore[attr-defined]


def test_a_link_in_the_photos_field_is_reported() -> None:
    """The report also has 2 photos, so nothing else would have complained."""
    result = parse(
        f"1. SOB | 0016\n2. INF\n3. {LINK}\n4. ",
        attachments=2,
    )

    assert "link_in_wrong_field" in codes(result)
    issue = next(i for i in result.issues if i.code == "link_in_wrong_field")  # type: ignore[attr-defined]
    assert issue.detail["found_in"] == "3"
    assert issue.detail["expected"] == "4"


def test_the_wrong_field_link_makes_the_report_invalid() -> None:
    """It used to validate, which is what let the mistake go unnoticed."""
    result = parse(f"1. SOB | 0016\n2. INF\n3. {LINK}\n4. ", attachments=2)

    assert result.matched  # type: ignore[attr-defined]
    assert not result.is_valid  # type: ignore[attr-defined]


def test_a_link_with_a_label_still_counts_as_a_link() -> None:
    result = parse(f"1. SOB | 0016\n2. INF\n3. Доказательства: {LINK}\n4. ", attachments=2)

    assert "link_in_wrong_field" in codes(result)


def test_the_correct_shape_is_silent() -> None:
    """A link in field 4 is the normal case and must produce no complaint."""
    result = parse(f"1. SOB | 0016\n2. INF\n3. фото\n4. {LINK}", attachments=2)

    assert "link_in_wrong_field" not in codes(result)
    assert result.is_valid  # type: ignore[attr-defined]


def test_a_link_in_both_places_is_not_flagged() -> None:
    """If the right field is already filled there is nothing to move."""
    result = parse(f"1. SOB | 0016\n2. INF\n3. {LINK}\n4. {LINK}", attachments=2)

    assert "link_in_wrong_field" not in codes(result)


def test_any_discord_link_is_accepted_without_being_fetched() -> None:
    """Presence is the check; the linked report's approval is not verified.

    Decided explicitly: senior instructors judge whether the linked report was approved
    while approving this one. So the parser must accept *any* well-formed Discord message
    link - including one in a guild this bot has never seen - and parsing is pure: it takes
    text and attachments and has no client to fetch anything with.
    """
    import inspect

    from dsbot.modules.reports import parser

    # Parsing must stay a pure function of the text: no client, no I/O.
    source = inspect.getsource(parser)
    assert "await " not in source, "the parser must not perform I/O"
    assert "discord.Client" not in source

    foreign = (
        "https://discord.com/channels/999999999999999999/888888888888888888/777777777777777777"
    )
    result = parse(f"1. SOB | 0016\n2. INF\n3. фото\n4. {foreign}", attachments=2)

    assert result.is_valid, "any well-formed Discord message link satisfies field 4"  # type: ignore[attr-defined]
    assert "link_not_discord" not in codes(result)
    from dsbot.modules.reports.templates import issue_line

    result = parse(f"1. SOB | 0016\n2. INF\n3. {LINK}\n4. ", attachments=2)
    issue = next(i for i in result.issues if i.code == "link_in_wrong_field")  # type: ignore[attr-defined]
    line = issue_line(
        issue.code,
        "ru",
        field_hint="?",
        **{f"detail_{k}": v for k, v in issue.detail.items()},
    )

    assert "пункте 3" in line
    assert "пункте 4" in line
    assert "Перенесите" in line
    # The wording must say what the link is FOR, not merely that one is wanted.
    assert "одобренный" in line
    assert "повышение квалификации" in line
    assert "{" not in line, "no unsubstituted placeholder may reach the channel"


def test_every_link_message_says_it_must_be_an_approved_report() -> None:
    """All three link codes must carry the same substantive requirement.

    "Field 4 contains no link" was technically true and practically useless: it did not say
    which link, or why. The requirement is a link to an *approved* report that grants the
    qualification promotion, so every link message says that.
    """
    from dsbot.modules.reports.templates import ISSUE_LINES

    for code in ("link_not_a_url", "link_not_discord", "link_in_wrong_field"):
        ru = ISSUE_LINES[code]["ru"]
        en = ISSUE_LINES[code]["en"]
        assert "одобренный" in ru, f"{code} must say the report has to be approved"
        assert "повышение квалификации" in ru, f"{code} must name the promotion"
        assert "approved" in en
        assert "promotion" in en
