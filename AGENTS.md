# AGENTS.md

Conventions and settled decisions for this repository. Read before changing architecture.

## What this project is

A Discord bot that passively monitors report channels in an RP military community,
parses reports written as numbered forms, tracks staff verdicts expressed as reactions,
and evaluates weekly quotas per character.

Phase 1 scope: **capture -> parse -> validate -> verdict -> weekly summary -> export**.
Explicitly out of scope for now: role assignment, soldier progression tiers,
soldier-to-instructor assignment, dashboards.

## Non-negotiable constraints

1. **No AI/LLM in the bot.** Parsing is deterministic. If parsing needs to become
   smarter, add rules and schema fields, not a model.
2. **No guessing.** If a domain rule is unknown, ask. Never invent a form layout,
   quota, rank name, or role.
3. **Nothing domain-specific in code.** Ranks, quotas, forms, and every user-visible
   string live in the database and are edited by Discord admins. Code holds zero
   rank names, zero counts, zero message bodies.
4. **Discord objects stop at `discord/sink.py`.** Everything downstream speaks
   `core/events.py` dataclasses. This is what makes replay, backfill, and tests share
   one code path.
5. **Modules never import each other.** A new feature subscribes to the event stream
   and owns its own tables.

## Domain model that must not be re-derived incorrectly

### Two identity domains

| | Actors | Subjects |
|---|---|---|
| Who | instructors, specialization command, high command | soldiers / characters |
| Key | Discord user id, in a guild | IDN |
| In the report guild | yes | no |
| Authority source | rank ladder, per guild | the report's field 1 |

### IDN is not unique

More than 10,000 characters share a 4-digit IDN space. Collisions are **expected
data**. Never add `UNIQUE` on an IDN column, and never reject a report because two
characters share a number. `idn_text` is preserved verbatim alongside the parsed parts.

An IDN has exactly **two** valid shapes. Never invent a third:

1. **plain**: exactly 4 digits - `SOB | 0016`
2. **prefixed**: 6 digits written `PP/00NN`, where `PP` is `01` or `00` and the two
   digits after the slash are always `00` - `SOB | 01/0016`

The optional system (NRP) IDN is exactly 4 digits and may follow the **prefixed** form
only. `SOB | 0016 | 0017` is invalid, and so is a bare 6-digit `SOB | 000016`.

Prefixes belong to SOB squads, so a prefixed IDN is rejected for any other formation
(`ARC | 01/0016` is invalid). `lore_prefixes` and `lore_prefix_formations` on the field
control both.

The slash is part of the identity, not formatting: the first two digits mean something
else entirely, so `01/0016` and `000016` are different namespaces and must never be
collapsed. Nobody writes a prefixed IDN without the slash. `canonical` therefore keeps
the separator: `SOB|01/0016` vs `SOB|0016`.

- `idn_kind` -> `plain` (4-digit) or `prefixed` (`PP/00NN`)

Squads (ALPHA, NULL) are **not** part of the form. They are internal structure of a
formation, and a reporter always writes the formation. `ALPHA | 01/0016` is rejected as
an unknown formation; the correct input is `SOB | 01/0016`.

JEDI are an exception that is **not yet implemented**: they identify soldiers by name
(Name + Surname) rather than by IDN. No Jedi reports reach this channel today, so the
current IDN-only identity field is correct for now. When Jedi reports do appear, JEDI
will need its own identity rule - do not force a name into an IDN field, and do not
assume the IDN shape applies to JEDI.

A Discord user may have several characters. Which one an instructor was acting as is
not inferable from a report and is not the bot's problem.

### Rank ordinal is not permission

`rank_tiers.ordinal` answers exactly one question: whose verdict outranks whose.
Capabilities live in `tier_capabilities`. Never infer "higher rank -> can administer".

Role ladders are defined per guild by admins (plain SQL rows), **not** taken from
Discord's role position ordering.

### Cross-server reactions

Approvers live in *specialization* servers, not the report server. A reactor from
another guild arrives as a `User`, with no `.roles`. Rank must be resolved via
`RankResolver` from the user's home guild and snapshotted onto the reaction row.
Never read `member.roles` at the reaction site.

### Verdicts are recomputed, not applied

`verdict = f(active reaction set, rank ordering)`. Un-reacting reverts to the next
highest surviving verdict automatically. Do not implement last-event-wins.

### A report is a message group

Evidence photos may arrive in a follow-up message by the same author. Validation must
therefore be deferrable, and a warning must clear itself when the missing piece lands.

### Attribution

`author_user_id` is always the message author. Who the report *counts for* is a
separate, resolved value (`attributed_*`), because a deputy may report for someone
else. Unresolvable attribution is a staff queue item, not a rejected report.

## Parsing pipeline

```
normalize -> tokenize -> select schema -> bind fields -> parse typed values -> validate
```

- Schemas are data (`FormSchemaDef`), serialised into `form_schemas.definition`.
- The parser must tolerate: blank values, `-` as an explicit blank, whitespace-only,
  skipped numbers, bold markers, inline `label: value`, and labels with no numbering.
- A malformed report is a **structured issue list**, never an exception. Raw message
  text is always stored regardless of parse outcome.
- Refuse to parse the template itself: `template_markers` disqualify a message.

## Engineering conventions

- Python 3.13, `uv` for everything (`uv run ...`, never bare `python`).
- SQLite in WAL mode with a **single serialized writer** (`Database.write_session`).
  All SQL lives in `storage/repos.py`; handlers and modules never build queries.
- All timestamps are timezone-aware UTC in the database.
- Log with `structlog`; console output is forced to UTF-8 because report text is
  Cyrillic and Windows consoles default to cp866.
- Ruff line length 100. `RUF001/002/003` are disabled: Cyrillic in source is expected.
- **Never rewrite source files with PowerShell.** `Get-Content`/`Set-Content` mangles
  UTF-8 Cyrillic and has corrupted source and tests repeatedly. Use the edit tool or a
  Python script. When reading command output set `$env:PYTHONIOENCODING="utf-8"`.

## Mistake log — do not repeat these

Each of these shipped and had to be found by a live run or a user report.

1. **`get_channel()` returns None for uncached channels.** It silently discarded every
   warning. Always fall back to `fetch_channel`, and log when a send fails.
2. **`uv run` leaves a child process alive.** Ctrl+C does not stop the bot, and the pid
   file can go stale while the real process keeps running. `stop` must scan for
   `dsbot run` processes, not just trust the pid.
3. **`ensure_schema_seed` overwrote the configured form** with the code default on
   every `bind`, silently wiping rules and value lists. The stored definition is the
   source of truth; seeding must only insert missing rows.
4. **`attachment_required` did not imply `accepts_attachments`.** The bot ignored every
   uploaded photo and reported correct reports as missing evidence.
5. **Two bots on one database** caused random behaviour and `UNIQUE` crashes. The lock
   exists to refuse the second instance.
6. **Deferred warnings were a dead end.** The timer was lost on restart. Warnings are
   immediate; a later edit clears them.
7. **Permission maths needs the Members intent.** Without it `guild.default_role` is
   None and `permissions_for` returns wrong answers. Prefer probing the real operation.

## Verification

```powershell
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run basedpyright
```

All three must be clean before considering work done.

## Before trusting the parser with live data

Run the analyzer against a real backfill dump:

```powershell
uv run dsbot backfill --channel <id> --since 14d --out backfill-out/ch.jsonl
uv run dsbot analyze --dump backfill-out/ch.jsonl
```

`unmatched` samples are the most valuable output: they show which real-world formats
the schema does not yet cover.
