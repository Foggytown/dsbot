# dsbot

Discord bot that passively monitors report channels in RP military communities,
parses reports written as numbered forms, tracks staff verdicts expressed as
reactions, and evaluates weekly quotas per character.

## Design in one page

```
Discord gateway
   |  discord.py objects are converted immediately; nothing downstream imports discord
   v
sink -> raw_events (append-only, idempotent by external_id)
   |
   v
EventBus (one FIFO lane per channel, so ordering is guaranteed)
   |
   +--> modules/reports   parse, validate, warn, clear warnings on fix
   +--> modules/verdict   pure function over the active reaction set
   +--> modules/quotas    weekly evaluation, per character
   +--> future modules    subscribe to the same stream, own their own tables
```

Three properties this buys:

- **Replay.** A parser bug is fixed by reprocessing `raw_events`, not by asking
  people to rewrite reports.
- **Backfill equals live.** `dsbot backfill` emits the same snapshots as the
  gateway, so a mid-week rollout produces data indistinguishable from live data.
- **One module never imports another.** Lifetime-count modules, promotion modules,
  and dashboards read the same stream and add their own tables.

## Two identity domains

| | Actors | Subjects |
|---|---|---|
| Who | Instructors, specialization command, high command | Soldiers / characters |
| Key | Discord user id in a guild | IDN (4 digits) |
| Present in the report guild | yes | no |
| Source of authority | Rank ladder (per guild, admin-defined) | The report's field 1 |

`IDN` is **not** unique: more than 10k characters share a 4-digit space. Collisions
are expected data, never a validation failure.

Rank `ordinal` answers exactly one question: *whose verdict outranks whose*.
Permissions are separate rows (`tier_capabilities`), so "higher rank" never
implicitly grants admin rights.

## Not in phase 1

Role assignment, soldier progression tiers, soldier-to-instructor assignment,
dashboards. Phase 1 is capture, parse, validate, verdict, weekly summary, export.

## Setup

```powershell
uv python install 3.13
uv sync
Copy-Item .env.example .env    # then paste your bot token
uv run dsbot init-db
```

Discord developer portal: enable **Message Content Intent** (required to read
report text) and **Server Members Intent** (required to resolve ranks).

## Commands

```powershell
uv run dsbot init-db
uv run dsbot bind --guild <guild> --channel <channel> --rank-guild <guild>
uv run dsbot backfill --channel <channel> --since 14d --out backfill-out/reports.jsonl
uv run dsbot analyze --dump backfill-out/reports.jsonl
uv run dsbot parse --file report.txt
uv run dsbot export --channel <channel> --out backfill-out/reports.csv
```

`backfill --to-db` writes to the database in the same pass. `--since` accepts an
ISO date or a relative duration (`14d`, `36h`).

`analyze` is the important one before going live: it runs the *same* parser over a
real dump and reports match rate, which fields are missing, and sample messages the
schema does not yet cover.

## Development

```powershell
uv run python scripts/check.py     # lint + format + types + tests
```

Or individually:

```powershell
uv run pytest
uv run ruff check --fix .
uv run ruff format .
uv run basedpyright
```
