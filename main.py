#!/usr/bin/env python3
"""Entry point for hosting panels that expect a `main.py`.

Most control panels (Pterodactyl, Pelican, and similar) assume the bot is started as
`python main.py` with the project root as the working directory. This file exists only
to adapt that convention to dsbot's actual CLI:

    python main.py              -> equivalent to `dsbot run`
    python main.py stop         -> `dsbot stop`
    python main.py <anything>   -> forwarded to the CLI verbatim

It deliberately avoids importing `dsbot` at module import time so that a missing
dependency produces a readable message instead of a bare traceback in the panel console.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Panels copy files around; make `src/` importable even when the package is not pip
# installed (e.g. `User Uploaded Files` + no git repository).
_SRC = Path(__file__).resolve().parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def main() -> int:
    try:
        from dsbot.cli import app
    except ModuleNotFoundError as exc:
        print(
            f"Missing dependency: {exc.name}\n"
            "Install the requirements first, for example:\n"
            "    pip install -r requirements.txt\n"
            "or run `uv sync` if uv is available.",
            file=sys.stderr,
        )
        return 1

    argv = sys.argv[1:]
    if not argv:
        argv = ["run"]

    # Typer reads sys.argv, so set it rather than calling internals.
    sys.argv = ["dsbot", *argv]
    app()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
