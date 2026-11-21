"""Regressions from the first real panel deployment.

Each of these shipped and cost a deployment:

  * the channels file was never applied, because the guard tested a setting that is
    deliberately empty by default, so the bot started serving nothing;
  * the database was written into the checkout instead of /data, where a redeploy
    deletes it - every report and verdict with it;
  * slash commands were only synced to guilds with a binding, so a stale registration
    from an earlier deploy was never corrected and users saw
    `CommandNotFound: Application command 'reports' not found`.

The panel stores its settings as a web form, not a file, so a bad value cannot be "fixed
by editing .env": the bot must cope with it and say what it did.
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
    assert str(s.resolved_channels_file()) != "", "resolving must not depend on it"
    assert "channels.yaml" in str(s.resolved_channels_file())


def test_an_explicit_channels_file_still_wins(tmp_path: Path) -> None:
    explicit = tmp_path / "custom.yaml"
    s = settings(data_dir="/app/data", channels_file=str(explicit))
    assert s.resolved_channels_file() == explicit


def test_a_database_inside_the_data_dir_is_accepted() -> None:
    s = settings(data_dir="/app/data")
    assert s.database_location_problem() is None
    assert s.ignored_database_url() is None


def test_the_panel_value_that_broke_the_deployment_is_ignored() -> None:
    """Copied verbatim from the hosting panel's variable list.

    `DSBOT_DATABASE_URL=sqlite+aiosqlite:///./var/dsbot.db` alongside `DATA_DIR=/app/data`
    put the database in the checkout while the channels file went to /app/data.
    """
    s = settings(data_dir="/app/data", database_url="sqlite+aiosqlite:///./var/dsbot.db")

    problem = s.database_location_problem()
    assert problem is not None
    assert "relative path" in problem
    assert "DSBOT_DATABASE_URL" in problem

    # The important part: the bad URL is not obeyed, and startup does not fail.
    assert s.ignored_database_url() == "sqlite+aiosqlite:///./var/dsbot.db"
    assert "/app/data" in s.resolved_database_url.replace("\\", "/")
    assert "var/dsbot.db" not in s.resolved_database_url.replace("\\", "/")


def test_an_absolute_path_elsewhere_is_a_warning_not_a_refusal() -> None:
    """A host that preserves an absolute path may be deliberate, so only warn."""
    s = settings(data_dir="/app/data", database_url="sqlite+aiosqlite:////srv/db/dsbot.db")
    assert s.database_location_problem() is None
    assert s.ignored_database_url() is None
    warning = s.database_location_warning()
    assert warning is not None
    assert "srv" in warning and "dsbot.db" in warning


def test_a_postgres_url_is_not_checked() -> None:
    """A real database server has its own storage; the rule is about local files."""
    s = settings(data_dir="/app/data", database_url="postgresql+asyncpg://u:p@h/db")
    assert s.database_location_problem() is None
    assert s.ignored_database_url() is None


def test_an_in_memory_database_is_not_checked() -> None:
    s = settings(data_dir="/app/data", database_url="sqlite+aiosqlite:///:memory:")
    assert s.database_location_problem() is None


def test_the_default_database_always_lands_in_the_state_dir() -> None:
    s = settings(data_dir="/app/data")
    assert "app" in s.resolved_database_url.replace("\\", "/")
    assert "data" in s.resolved_database_url.replace("\\", "/")
