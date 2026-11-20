"""Persistent state must live where the hosting panel keeps it.

A panel preserves exactly one directory across redeploys. If the database is written
outside it, every restart wipes all reports, verdicts and weekly results. These tests pin
the resolution order.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dsbot.config import Settings


def settings(**kwargs: object) -> Settings:
    return Settings(_env_file=None, **kwargs)  # type: ignore[call-arg]


def test_local_default_is_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a panel the state stays where developers expect it."""
    monkeypatch.delenv("DATA_DIR", raising=False)
    monkeypatch.delenv("DSBOT_DATA_DIR", raising=False)
    config = settings()
    assert config.state_dir() == Path("var")
    assert "var" in config.resolved_database_url


def test_data_dir_env_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    """`DATA_DIR` is the panel convention, with no DSBOT_ prefix."""
    monkeypatch.setenv("DATA_DIR", "/data")
    config = settings()
    assert config.state_dir() == Path("/data")
    assert config.resolved_database_url.endswith("/data/dsbot.db")


def test_explicit_setting_wins_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_DIR", "/data")
    config = settings(data_dir="/custom")
    assert config.state_dir() == Path("/custom")


def test_explicit_database_url_wins(tmp_path: Path) -> None:
    """A Postgres URL or a hand-placed file is never overridden."""
    config = settings(
        data_dir=str(tmp_path),
        database_url="postgresql+asyncpg://user:pass@host/db",
    )
    assert config.resolved_database_url == "postgresql+asyncpg://user:pass@host/db"
    assert config.is_sqlite is False


def test_lock_lives_in_the_state_dir(tmp_path: Path) -> None:
    """The lock and pid file must survive too, or a restart cannot find the old pid."""
    config = settings(data_dir=str(tmp_path))
    assert config.lock_file() == tmp_path / "dsbot.lock"
    assert config.pid_file() == tmp_path / "dsbot.pid"


def test_lock_path_override_still_wins(tmp_path: Path) -> None:
    config = settings(data_dir="/ignored", lock_path=str(tmp_path / "x.lock"))
    assert config.lock_file() == tmp_path / "x.lock"


def test_instance_name_selects_the_lock_file(tmp_path: Path) -> None:
    prod = settings(data_dir=str(tmp_path), instance_name="prod")
    test = settings(data_dir=str(tmp_path), instance_name="test")
    assert prod.lock_file() != test.lock_file()


def test_database_url_is_inside_the_state_dir(tmp_path: Path) -> None:
    """The core guarantee: state dir in, database file in the same place."""
    config = settings(data_dir=str(tmp_path))
    url = config.resolved_database_url
    assert "sqlite" in url
    assert tmp_path.as_posix() in url
