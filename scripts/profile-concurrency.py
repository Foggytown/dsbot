"""Before/after of the reaction reads, using the real bounded-concurrency helper."""

from __future__ import annotations

import asyncio
import time

from dsbot.modules.quotas.live import _gather_bounded

N = 400
LATENCY = 0.12
LIMIT = 8


async def call() -> float:
    started = time.perf_counter()
    await asyncio.sleep(LATENCY)
    return time.perf_counter() - started


async def main() -> None:
    started = time.perf_counter()
    for _ in range(N):
        await call()
    sequential = time.perf_counter() - started

    started = time.perf_counter()
    await _gather_bounded([call() for _ in range(N)], limit=LIMIT)
    bounded = time.perf_counter() - started

    started = time.perf_counter()
    await asyncio.gather(*(call() for _ in range(N)))
    unbounded = time.perf_counter() - started

    print(f"{N} reaction reads at {LATENCY * 1000:.0f} ms each")
    print(f"  sequential (old):   {sequential:6.2f} s")
    print(f"  bounded x{LIMIT}:       {bounded:6.2f} s   ({sequential / bounded:.0f}x faster)")
    print(f"  unbounded (risky):  {unbounded:6.2f} s   (429s in practice)")


if __name__ == "__main__":
    asyncio.run(main())
