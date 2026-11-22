"""The empty-segment hole, on the parser the channel actually uses.

`SOB || 0016` passed as valid for months while a test claimed it was rejected, because the
test called `parse_formation_idn` - a parser reachable only from the legacy
`DEFAULT_SCHEMAS`. The real form uses `type: identity`, which goes through `parse_identity`
and silently dropped empty segments. That is AGENTS #38: a test written from the same wrong
assumption as the code, green while the hole is open.

`SOB | 01/0016 || 0017` was the worst of them: the empty slot vanished, the system IDN slid
into its place, and the report validated with the second ID reported as the first.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from dsbot.modules.reports.parser import (
    GroupMessage,
    ParseOutput,
    ReportCandidate,
    parse_report,
)
from dsbot.modules.reports.yaml_form import parse_form_file

LINK = "https://discord.com/channels/1/2/3"


def parse_identity_field(identity: str) -> ParseOutput:
    """Parse one report through the channel's real form, not a legacy default."""
    form = parse_form_file(Path("forms/training.yaml"))
    text = f"1. {identity}\n2. INF\n3. фото\n4. {LINK}"
    candidate = ReportCandidate(
        messages=[
            GroupMessage(
                message_id=1,
                author_id=1,
                created_at=datetime.now(UTC),
                content=text,
                attachment_count=2,
            )
        ]
    )
    return parse_report([form], candidate, attachment_count=2)


@pytest.mark.parametrize(
    "identity",
    [
        "SOB || 0016",
        "SOB | | 0016",
        "SOB | 0016 |",
        "SOB | 01/0016 || 0017",
        "|| 0016",
    ],
)
def test_an_empty_slot_is_reported_not_repaired(identity: str) -> None:
    result = parse_identity_field(identity)

    codes = [issue.code for issue in result.issues]
    assert "identity_bad_format" in codes, f"{identity!r} was accepted: {codes}"
    assert not result.is_valid


@pytest.mark.parametrize(
    "identity",
    [
        "SOB | 0016",
        "SOB | 0016 | 0017",
        # A prefixed IDN requires its system IDN, so it is valid only with both parts.
        "SOB | 01/0016 | 0017",
    ],
)
def test_the_valid_shapes_still_pass(identity: str) -> None:
    """Rejecting empty slots must not reject the forms that are legitimate."""
    result = parse_identity_field(identity)

    assert result.is_valid, f"{identity!r} was rejected: {result.issues}"


def test_a_slid_system_idn_is_caught() -> None:
    """The empty slot must not let the system IDN be mistaken for the lore one."""
    result = parse_identity_field("SOB | 01/0016 || 0017")

    assert not result.is_valid
    # And the report must not silently record a canonical identity as if all was well.
    identity_field = next(bound for bound in result.fields if bound.spec.id == "identity")
    value = identity_field.result.value or {}
    assert value.get("canonical") is None or value.get("nrp_idn") is None
