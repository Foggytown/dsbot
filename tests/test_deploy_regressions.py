"""Regressions from the first real panel deployment.

Each of these shipped and cost a deployment:

  * the channels file was never applied, because the guard tested a setting that is
    deliberately empty by default, so the bot started serving nothing;
  * the database was written into the checkout instead of /data, where a redeploy
    deletes it - every report and verdict with it;
  * slash commands were only synced to guilds with a binding, so a stale registration
    from an earlier deploy was never corrected and users saw
    `CommandNotFound: Application command 'reports' not found`.
"""

from __future__ import annotations

from pathlib import Path

from dsbot.config import Settings


def settings(**kwargs: object) -> Settings:
    return Settings(_env_file=None, **kwargs)  # type: ignore[call-arg]


def test_channels_file_is_derived_from_the_data_dir_not_a_setting() -> None:
    """The setting is empty by default; the real path must still resolve.

    The bug: startup did `if settings.channels_file`, which was False, so the channels
    file was never applied and no channel was ever bound.
    """
    s = settings(data_dir="/app/data")
    assert s.channels_file == "", "the setting is meant to be empty by default"
    assert s.resolved_channels_file() == Path("/app/data/channels.yaml")
    assert str(s.resolved_channels_file()) != "", "resolving must not depend on the setting"


def test_an_explicit_channels_file_still_wins(tmp_path: Path) -> None:
    explicit = tmp_path / "custom.yaml"
    s = settings(data_dir="/app/data", channels_file=str(explicit))
    assert s.resolved_channels_file() == explicit


def test_a_database_inside_the_data_dir_is_accepted() -> None:
    s = settings(data_dir="/app/data")
    assert s.database_location_problem() is None


def test_an_absolute_database_inside_the_data_dir_is_accepted() -> None:
    import tempfile

    data = Path(tempfile.mkdtemp())
    s = settings(
        data_dir=str(data),
        database_url=f"sqlite+aiosqlite:///{(data / 'dsbot.db').as_posix()}",
    )
    assert s.database_location_problem() is None
    assert s.database_location_warning() is None


def test_a_database_in_the_checkout_is_refused(tmp_path: Path) -> None:
    """The exact panel misconfiguration: DSBOT_DATABASE_URL=./var/... with /app/data.

    A redeploy replaces the checkout, so this silently deletes all history.
    """
    s = settings(data_dir=str(tmp_path / "data"), database_url="sqlite+aiosqlite:///./var/x.db")
    problem = s.database_location_problem()
    assert problem is not None
    # A relative path always resolves against the checkout, so it is fatal regardless of
    # where the data directory happens to be.
    assert "relative path" in problem
    assert "DSBOT_DATABASE_URL" in problem, "the message must name the setting to remove"


def test_a_postgres_url_is_not_checked() -> None:
    """A real database server has its own storage; the rule is about local files."""
    s = settings(data_dir="/app/data", database_url="postgresql+asyncpg://u:p@h/db")
    assert s.database_location_problem() is None


def test_an_in_memory_database_is_not_checked() -> None:
    s = settings(data_dir="/app/data", database_url="sqlite+aiosqlite:///:memory:")
    assert s.database_location_problem() is None


def test_the_default_database_always_lands_in_the_state_dir() -> None:
    s = settings(data_dir="/app/data")
    assert "/app/data" in s.resolved_database_url.replace("\\", "/")


def test_the_panel_value_that_broke_the_deployment_is_refused() -> None:
    """Copied verbatim from the hosting panel's variable list.

    DSBOT_DATABASE_URL=sqlite+aiosqlite:///./var/dsbot.db alongside DATA_DIR=/app/data is
    what put the database in the checkout while the channels file went to /app/data.
    """
    s = settings(data_dir="/app/data", database_url="sqlite+aiosqlite:///./var/dsbot.db")
    assert s.database_location_problem() is not None


def test_an_absolute_path_elsewhere_is_a_warning_not_a_refusal() -> None:
    """A host that preserves an absolute path may be deliberate, so only warn."""
    s = settings(data_dir="/app/data", database_url="sqlite+aiosqlite:////srv/db/dsbot.db")
    assert s.database_location_problem() is None
    warning = s.database_location_warning()
    assert warning is not None
    # Compare with normalised separators: Windows renders the POSIX path with backslashes.
    assert "srv" in warning and "dsbot.db" in warning
