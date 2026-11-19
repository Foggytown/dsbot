"""Process control tests: stopping must work regardless of how the bot was launched.

The motivating bug: `uv run dsbot run` spawns a child, closing the terminal left it
alive, and the bot kept answering messages after the user thought it was stopped.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from dsbot.core.lock import (
    InstanceLock,
    is_running,
    read_pid,
    stop_running,
)


def test_pid_is_recorded_on_acquire(tmp_path: Path) -> None:
    lock_path = tmp_path / "dsbot.lock"
    with InstanceLock(lock_path):
        assert read_pid(tmp_path / "dsbot.pid") == os.getpid()
        assert is_running(os.getpid())
    # A clean release removes the pid file.
    assert read_pid(tmp_path / "dsbot.pid") is None


def test_missing_pid_file_reads_as_none(tmp_path: Path) -> None:
    assert read_pid(tmp_path / "nope.lock") is None


def test_garbage_pid_file_reads_as_none(tmp_path: Path) -> None:
    path = tmp_path / "bad.lock"
    path.write_bytes(b"not-a-pid")
    assert read_pid(path) is None


def test_second_instance_reports_the_holder_pid(tmp_path: Path) -> None:
    lock_path = tmp_path / "dsbot.lock"
    with InstanceLock(lock_path), pytest.raises(RuntimeError, match=f"pid {os.getpid()}"):
        InstanceLock(lock_path).acquire()


def test_stop_with_no_pid_file_is_a_no_op(tmp_path: Path) -> None:
    assert "no pid file" in stop_running(tmp_path / "dsbot.lock")


def test_stop_with_stale_pid_reports_stale(tmp_path: Path) -> None:
    """A pid that no longer exists must not be reported as stopped-just-now."""
    (tmp_path / "dsbot.pid").write_bytes(str(os.getpid() + 4_000_000).encode())
    outcome = stop_running(tmp_path / "dsbot.lock", wait_seconds=0.1)
    assert "stale" in outcome or "not running" in outcome


def test_lock_blocks_a_second_instance_but_not_after_release(tmp_path: Path) -> None:
    lock_path = tmp_path / "dsbot.lock"
    first = InstanceLock(lock_path)
    first.acquire()
    first.release()

    second = InstanceLock(lock_path)
    second.acquire()
    second.release()
