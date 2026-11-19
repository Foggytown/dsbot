#!/usr/bin/env python3
"""One-shot check: tests, lint, format, types. Fails on the first problem."""

from __future__ import annotations

import subprocess
import sys

STEPS: list[tuple[str, list[str]]] = [
    ("ruff check", ["ruff", "check", "."]),
    ("ruff format", ["ruff", "format", "--check", "."]),
    ("basedpyright", ["basedpyright"]),
    ("pytest", ["pytest"]),
]


def main() -> int:
    for name, command in STEPS:
        print(f"\n=== {name} ===", flush=True)
        result = subprocess.run(["uv", "run", *command], check=False)
        if result.returncode != 0:
            print(f"\nFAILED: {name}", file=sys.stderr)
            return result.returncode
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
