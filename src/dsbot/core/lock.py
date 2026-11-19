"""Single-instance guard.

Running two bot processes against one database is actively harmful: both receive the
same gateway events, both write, and the loser of an insert race used to crash. It also
makes behaviour look random, because some messages are handled by one process and some
by the other.

The lock is a file opened with an exclusive advisory lock (`msvcrt` on Windows, `fcntl`
elsewhere) and released automatically when the process dies, so a crash never leaves a
stale lock behind.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import TracebackType


class InstanceLock:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: object | None = None

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Unbuffered binary mode: msvcrt locks a byte range, and buffered text IO can
        # leave the file position somewhere that makes the unlock fail.
        handle = open(self._path, "a+b")  # noqa: SIM115
        try:
            handle.seek(0)
            _lock(handle)
        except OSError:
            handle.close()
            raise RuntimeError(
                f"another dsbot instance is already running (lock file: {self._path}).\n"
                "Only one bot may run per database. Stop the other instance, or check "
                "for a stray `dsbot run` with: "
                "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\""
            ) from None
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()).encode())
        handle.flush()
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            _unlock(handle)  # type: ignore[arg-type]
        finally:
            handle.close()  # type: ignore[attr-defined]
            self._handle = None

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
