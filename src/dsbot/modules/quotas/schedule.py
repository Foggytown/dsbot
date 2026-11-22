"""When a channel's reporting period closes, and what to do about it.

The model is deliberately small: a channel stores **when the current period ends** and
**how long a period is**. Everything else follows.

    next_close = 22.11.2026 23:59:59   (in the channel's timezone)
    period_days = 7

    -> the period being counted is [15.11 23:59:59, 22.11 23:59:59)
    -> after it is published, next_close becomes 29.11.2026 23:59:59

That is one number an admin can reason about, instead of a cron expression plus a rule for
which period a run belongs to. It also makes "the admin set the close a bit later this
time" a single field change rather than a schedule rebuild.

Pure functions only: no database, no Discord. The scheduler and the slash command both
call into this module so they cannot disagree about which period a run covers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from dsbot.modules.quotas.period import Period, load_timezone

# A period boundary is inclusive of its closing second as far as a human is concerned:
# "the period ends at 23:59:59" means a report posted at 23:59:59 is still inside it. The
# stored end is therefore one second after the closing instant, which keeps `[start, end)`
# half-open internally while doing what the admin wrote.
CLOSE_SLACK = timedelta(seconds=1)

# How late a close may still be published.
#
# The admin asked for "skip a period the bot missed", and that is the default behaviour:
# a bot that was down for three days must not dump a stale report into the channel. But a
# redeploy takes a minute, and skipping a whole week because the process restarted 40
# seconds after the boundary would be a much worse bug than the one this guards against.
# So a close is still honoured inside this window, and skipped outside it.
DEFAULT_GRACE = timedelta(hours=2)

ALLOWED_PERIOD_DAYS = (7,)


@dataclass(frozen=True, slots=True)
class Schedule:
    """A channel's close schedule. Times are aware UTC."""

    period_days: int
    next_close: datetime

    def __post_init__(self) -> None:
        if self.period_days < 1:
            raise ValueError("period must be at least one day")
        if self.next_close.tzinfo is None:
            raise ValueError("next_close must be timezone-aware")

    @property
    def period_length(self) -> timedelta:
        return timedelta(days=self.period_days)

    @property
    def current_period(self) -> Period:
        """The period ending at `next_close`, inclusive of the closing instant.

        The interval is `[close - length, close]`, advanced by one second only to make it
        half-open for counting, so a report posted at exactly `close` still belongs to the
        period.

        The admin's close is a **moment, not a date**. An earlier version added a second
        and then assumed the boundary was midnight, which produced `15.11–22.11` for a
        `22.11 17:00` close - a whole day early, for any close time that is not midnight.
        Do not reason about "the closing day" here.
        """
        end = self.next_close + CLOSE_SLACK
        return Period(start=end - self.period_length, end=end)

    def rolled(self, *, after: datetime) -> Schedule:
        """The schedule whose next close is the first one strictly after `after`.

        Advancing by whole periods keeps every boundary on the same clock time, so a
        schedule set to 23:59:59 does not drift to 00:00:07 after a few months.
        """
        close = self.next_close
        while close <= after:
            close += self.period_length
        return Schedule(period_days=self.period_days, next_close=close)


@dataclass(frozen=True, slots=True)
class Due:
    """What a check at a given moment concludes.

    `action` is one of:
      * `wait`    - nothing to do yet
      * `publish` - this period has closed and is still fresh enough to report
      * `skip`    - this period closed too long ago to be worth reporting
    """

    action: str
    period: Period | None = None
    next_close: datetime | None = None
    skipped_periods: int = 0


def due_at(schedule: Schedule, now: datetime, *, grace: timedelta = DEFAULT_GRACE) -> Due:
    """Decide what to do at `now`.

    Only ever reports **one** period, the one that just closed. A bot that was down for
    six weeks does not publish six reports; it either publishes the latest close if it is
    still inside the grace window, or skips and moves on.
    """
    if now < schedule.next_close:
        return Due(action="wait")

    period = schedule.current_period
    rolled = schedule.rolled(after=now)
    skipped = _periods_between(schedule.next_close, rolled.next_close, schedule.period_length)

    if now > schedule.next_close + grace:
        return Due(
            action="skip",
            period=period,
            next_close=rolled.next_close,
            skipped_periods=skipped,
        )

    return Due(
        action="publish",
        period=period,
        next_close=rolled.next_close,
        skipped_periods=skipped,
    )


def _periods_between(start: datetime, end: datetime, length: timedelta) -> int:
    """How many boundaries passed, for logging an honest 'we skipped N'."""
    if end <= start:
        return 0
    return max(0, round((end - start) / length))


def parse_close(value: str, timezone: str) -> datetime:
    """Read what an admin typed as a closing moment.

    Accepts `22.11.2026 23:59:59`, `22.11.2026 23:59`, `22.11.2026`, and the ISO forms.
    A bare date means the very end of that day, because "the period ends on the 22nd"
    should include the 22nd, not stop at its first second.
    """
    text = (value or "").strip()
    if not text:
        raise ValueError("give a closing date, for example 22.11.2026 23:59:59")

    zone = load_timezone(timezone)
    moment = _parse_moment(text)
    if moment is None:
        raise ValueError(
            f"could not read the date {value!r}. Use DD.MM.YYYY (22.11.2026), "
            "optionally with a time (22.11.2026 23:59:59), or YYYY-MM-DD."
        )
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=zone)
    return moment.astimezone(UTC)


def _parse_moment(text: str) -> datetime | None:
    """Datetime first, then a bare date treated as the last second of that day.

    `datetime.fromisoformat` only learned to read `22.11.2026 23:59:59` in Python 3.11 and
    still refuses the dotted Russian date, so the parts are parsed explicitly. This is the
    spelling an admin actually types, so it must not be the spelling that fails.
    """
    for candidate in (text, text.replace(" ", "T")):
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            continue

    # DD.MM.YYYY with an optional time. A comma between date and time is tolerated
    # because that is how it is written in prose.
    parts = text.replace(",", " ").split()
    if not parts:
        return None
    try:
        day = _parse_dotted_date(parts[0])
    except ValueError:
        return None
    if len(parts) == 1:
        return datetime.combine(day, time(23, 59, 59))
    return _with_clock(day, parts[1])


def _parse_dotted_date(text: str) -> date:
    day, month, year = text.replace("/", ".").split(".")
    return date(int(year), int(month), int(day))


def _with_clock(day: date, clock: str) -> datetime | None:
    """`23:59` and `23:59:59` both work; anything else is rejected, not guessed."""
    pieces = clock.split(":")
    try:
        numbers = [int(piece) for piece in pieces]
    except ValueError:
        return None
    if len(numbers) == 2:
        numbers.append(0)
    if len(numbers) != 3:
        return None
    try:
        hour, minute, second = numbers
        return datetime.combine(day, time(hour, minute, second))
    except ValueError:
        return None


def describe_close(moment: datetime, timezone: str) -> str:
    """Render a close back to the admin in their own timezone."""
    local = moment.astimezone(load_timezone(timezone))
    return f"{local:%d.%m.%Y %H:%M:%S}"
