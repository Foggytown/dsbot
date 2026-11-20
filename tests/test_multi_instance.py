"""Two instances side by side: different name, different DB, independent stop."""

from __future__ import annotations

from pathlib import Path

from dsbot.config import Settings
from dsbot.core.lock import (
    InstanceLock,
    find_dsbot_processes,
    is_running,
    read_pid,
    stop_all,
)


def settings(name: str, db: str) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        instance_name=name,
        database_url=f"sqlite+aiosqlite:///{db}",
    )


def test_instance_names_give_different_lock_files() -> None:
    prod = settings("prod", "var/prod.db")
    test = settings("test", "var/test.db")
    assert prod.lock_file() != test.lock_file()
    assert prod.pid_file() != test.pid_file()
    assert prod.lock_file().name == "prod.lock"
    assert test.lock_file().name == "test.lock"


def test_explicit_lock_path_wins() -> None:
    custom = Settings(
        _env_file=None,  # type: ignore[call-arg]
        instance_name="prod",
        lock_path="var/custom.lock",
    )
    assert custom.lock_file() == Path("var/custom.lock")
    assert custom.pid_file() == Path("var/custom.pid")


def test_default_instance_name_is_backwards_compatible() -> None:
    """An existing .env without the variable keeps the old file names."""
    default = Settings(_env_file=None)  # type: ignore[call-arg]
    assert default.instance_name == "dsbot"
    assert default.lock_file() == Path("var/dsbot.lock")


def test_two_instances_can_hold_their_own_locks(tmp_path: Path) -> None:
    """The whole point: a second lock file does not block the first."""
    first = InstanceLock(tmp_path / "prod.lock")
    second = InstanceLock(tmp_path / "test.lock")
    first.acquire()
    try:
        second.acquire()
        try:
            # Both held at once, which is what allows a test bot next to the live one.
            assert read_pid(tmp_path / "prod.pid") is not None
            assert read_pid(tmp_path / "test.pid") is not None
        finally:
            second.release()
    finally:
        first.release()

    # Releasing one must not clear the other.
    first.acquire()
    try:
        second.acquire()
        second.release()
        assert read_pid(tmp_path / "prod.pid") is not None
    finally:
        first.release()


def test_same_lock_file_still_refuses_a_second_instance(tmp_path: Path) -> None:
    """Two processes on ONE database remain forbidden."""
    import pytest

    lock = InstanceLock(tmp_path / "prod.lock")
    lock.acquire()
    try:
        with pytest.raises(RuntimeError, match="already running"):
            InstanceLock(tmp_path / "prod.lock").acquire()
    finally:
        lock.release()


def test_stop_without_pid_file_reports_clearly(tmp_path: Path) -> None:
    outcome = stop_all(lock_path=tmp_path / "nothing.lock")
    assert "no pid file" in outcome


def test_process_scan_returns_the_current_pid_out_of_the_list() -> None:
    """The scan must not offer to kill the process doing the scanning."""
    import os

    found = find_dsbot_processes()
    assert os.getpid() not in found


def test_is_running_handles_missing_pid() -> None:
    assert is_running(None) is False
    assert is_running(0) is False
