"""Process control: single-instance lock, PID tracking, and stopping a running bot.

The problem this solves: `uv run dsbot run` spawns a child process. Closing the terminal,
pressing Ctrl+C in the wrong window, or killing `uv` leaves the actual bot orphaned and
still answering messages. Stopping must therefore not depend on the terminal at all.
"""

from __future__ import annotations

import contextlib
import os
import signal
import sys
import time
from pathlib import Path
from types import TracebackType

import structlog

log = structlog.get_logger(__name__)

DEFAULT_LOCK_PATH = Path("var/dsbot.lock")
DEFAULT_PID_PATH = Path("var/dsbot.pid")


class InstanceLock:
    """Exclusive advisory lock plus a separate PID file.

    The lock and the pid live in different files because a locked byte range on Windows
    blocks *reads* too, so the pid could never be inspected while the bot was running.
    """

    def __init__(self, path: Path = DEFAULT_LOCK_PATH, pid_path: Path | None = None) -> None:
        self._path = path
        self._pid_path = pid_path or path.with_suffix(".pid")
        self._handle: object | None = None

    @property
    def pid_path(self) -> Path:
        return self._pid_path

    @property
    def path(self) -> Path:
        return self._path

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Binary unbuffered: msvcrt locks a byte range, and text buffering can move the
        # file position so that the unlock fails with a confusing PermissionError.
        handle = open(self._path, "a+b")  # noqa: SIM115
        try:
            handle.seek(0)
            _lock(handle)
        except OSError:
            handle.close()
            holder = read_pid(self._pid_path)
            where = f" (pid {holder})" if holder else ""
            raise RuntimeError(
                f"another dsbot instance is already running{where}.\n"
                "Only one bot may run per database. Stop it with:\n"
                "    uv run dsbot stop"
            ) from None
        self._pid_path.parent.mkdir(parents=True, exist_ok=True)
        self._pid_path.write_bytes(str(os.getpid()).encode())
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            with_suppress_unlock(handle)
        finally:
            handle.close()  # type: ignore[attr-defined]
            self._handle = None
            # Remove the pid file so `dsbot status` reports accurately after a clean exit.
            with contextlib.suppress(OSError):
                self._pid_path.unlink()

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


def with_suppress_unlock(handle: object) -> None:
    # The lock is released when the handle closes regardless; an explicit unlock
    # failure must not mask the real shutdown path.
    with contextlib.suppress(OSError):
        _unlock(handle)


def read_pid(path: Path = DEFAULT_LOCK_PATH) -> int | None:
    try:
        raw = path.read_bytes().strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        return int(raw.decode("ascii", "ignore"))
    except ValueError:
        return None


def clear_pid(path: Path = DEFAULT_PID_PATH) -> None:
    """Remove a pid file. Used after a crash so a recycled pid cannot be signalled."""
    with contextlib.suppress(OSError):
        path.unlink()


def is_running(pid: int | None) -> bool:
    """True if the pid exists. Uses a real signal probe, not a guess."""
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    except Exception:
        return False
    return True


def stop_running(path: Path = DEFAULT_LOCK_PATH, *, wait_seconds: float = 15.0) -> str:
    """Stop the bot owning `path`. Returns a human-readable outcome.

    Escalates: polite signal, then forceful termination. Ctrl+C on Windows is not
    reliably delivered to a detached child, so termination is the dependable path.
    """
    pid = read_pid(path.with_suffix(".pid"))
    if pid is None:
        return "not running (no pid file)"
    if not is_running(pid):
        return f"not running (stale pid {pid})"

    with contextlib.suppress(OSError):
        _signal_polite(pid)

    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if not is_running(pid):
            return f"stopped (pid {pid})"
        time.sleep(0.25)

    try:
        _signal_force(pid)
    except OSError:
        return f"could not signal pid {pid}; kill it manually"

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if not is_running(pid):
            return f"force-stopped (pid {pid})"
        time.sleep(0.25)
    return f"still running after force kill (pid {pid})"


def _signal_polite(pid: int) -> None:
    if sys.platform == "win32":
        # CTRL_BREAK reaches console-attached children; harmless if it does not land.
        try:
            os.kill(pid, signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            raise
    else:
        os.kill(pid, signal.SIGTERM)


def _signal_force(pid: int) -> None:
    os.kill(pid, signal.SIGTERM)


def _lock(handle: object) -> None:
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]


def _unlock(handle: object) -> None:
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)  # type: ignore[attr-defined]
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]
