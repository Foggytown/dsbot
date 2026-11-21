# AGENTS.md

Conventions and settled decisions for this repository. Read before changing architecture.

## What this project is

A Discord bot that passively monitors report channels in an RP military community,
parses reports written as numbered forms, tracks staff verdicts expressed as reactions,
and evaluates weekly quotas per character.

Deployed phases:

- Phase 1 (done): capture -> parse -> validate -> warnings -> export.
- **Phase 2 (done):** cross-server rank resolution, verdict engine from reactions.
- **Phase 3 (done):** approved-report counting per person per week, roster scraping,
  slash command, CSV and optional publication.
- **Next:** scheduled weekly posting, dashboards.

Explicitly out of scope for now: role assignment, soldier progression tiers,
soldier-to-instructor assignment, dashboards.

## What lives where

| Data | Location | Why |
|---|---|---|
| Messages, reports, reactions, verdicts, weekly results | server database | change constantly, server only |
| Forms | `forms/*.yaml` | configuration, reviewed in git |
| Rank ladders | `forms/ladders/*.yaml` | configuration, one file per ladder |
| Served channels | `<state dir>/channels.yaml` | edited on the server, applies live |
| Token and deployment settings | `.env` on the server | secret, never committed |

Config is applied at startup, so a locally configured channel is deployed by pushing.
There is nothing to merge between machines: runtime data exists only on the server.

Persistent state must live in the panel data directory (`/data`). Resolution order:
`DSBOT_DATA_DIR`, `DATA_DIR`, an existing `/data` or `/app/data`, then `./var`.

### Only `/data` survives a deploy

The panel replaces the whole checkout on every upload and keeps only `/data`. Therefore
**every** piece of wiring — which channel, which form, which review ladder, which quota
ladder — must come from git and be rebuilt on each start. A binding or ladder created by
hand on the server is lost or duplicated on the next upload.

`forms/channels.yaml` is the single source of that wiring: it names `form`,
`review_ladder` and `quota_ladder` per channel, and startup applies all of them, every
time (not only when the database is empty). `ladder-apply` is therefore optional, for
applying a file to a running bot without a restart.

**The live channels file is in the data directory, not the checkout.** Because the
checkout is replaced on each deploy, a channel list inside it would reset every time.
`forms/channels.yaml` is only a seed, copied to `<state dir>/channels.yaml` once; after
that the file in `/data` is authoritative and is never overwritten. The bot polls its
modification time and re-applies it within seconds, so an admin can add or remove a
channel on the server without a restart or a redeploy. A broken edit is logged and
ignored: the previous configuration keeps running.

Because sync runs on every start, an **unchanged ladder must not be rebuilt**: deleting
its row would cascade-delete `roster_members`, and the weekly report would claim nobody
is tracked after each deploy. `replace_ladder` compares content and keeps an identical
ladder in place.

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

### Who may approve

Only a tier whose `capabilities` include `review`. A reviewer may approve **their own**
report; that is expected, not an error to "fix". A reaction whose author's rank could
not be resolved is stored but counts for nothing — a NULL `can_review` must never be
treated as "allowed", or any stranger's check mark would approve a report.

Two reviewers of the **same** ordinal who disagree produce `conflict`. Different
ordinals are resolved by the higher one. A conflict is never guessed at.

### Review authority and quotas are separate ladders

This is the correction that matters most:

> Roles that approve are NOT the roles that owe reports. Different tiers, often
> different roles entirely.

So a guild has several ladders, distinguished by a declared `purpose`:

| `purpose` | Contains | Carries | Used for |
|---|---|---|---|
| `review` | who may approve | `capabilities` | verdicts, `/reports count` permission |
| `quota` | who owes reports | `required_per_week` | the weekly table |

A quota tier sets `required_per_week` and **must not** list `capabilities`;
`ladder-check` rejects it, and a review tier that sets `required_per_week` is flagged
because the number would be silently ignored. The reactor path asks the resolver for
`requires={"review"}`, so a quota role physically cannot decide a verdict however high
its ordinal is. Do not collapse these into one ladder: doing so either lets everyone who
reports approve themselves, or drops the reporting roles out of the count.

**One requirement per person, not a sum.** Someone holding several quota roles gets the
requirement of the highest `ordinal`. A promotion must not stack a second requirement on
top of the first, and a leftover junior role must not drag the duty back down. This is
implemented by `GuildLadders.required_for_roles` and frozen onto the roster row.

`required_per_week` lives on the tier (and in the YAML file) because it is a property of
the rank. `quota_policies` exists only for the exception: one channel needing a different
number than the ladder says. Never require an admin to retype every number per channel.

### Hierarchy is per channel, roles are cross-server

Two different servers, two different jobs:

- **where the roles live**: `channel_bindings.rank_source_guild_id`. Normally a
  specialization server, not the server where reports are read. This is what makes
  cross-server approval work at all.
- **which ordering applies**: `channel_bindings.rank_ladder_id`. When set, that channel
  is ordered only by that ladder. When unset, every ladder in the roles' guild competes
  and the highest ordinal wins.

So two channels can read reports in one guild while ordering the same people
differently, and the same person can be a reviewer in one channel and a reporter in
another. Never infer the hierarchy from Discord's role position ordering.

### What counting counts

Counting answers one question: how many reports did this person file in the period.

**Reports come from Discord history, not from the database.** The archive only contains
what the bot happened to see while running: ask it for last week when the bot started
yesterday and the honest answer is "nothing", which looks identical to "nobody reported".
So `/reports count` reads the channel's real messages for the requested window, parses
them with the same parser the live bot uses, reads each message's reactions, and counts.
Nothing is written back.

The database is still needed, for three things it is genuinely better at:

- **form and ladder configuration** (applied from git, cached in the DB)
- **the roster and quota norms** (who was supposed to report, and how many)
- **an audit trail** of what was seen, including invalid reports and their warnings

The one honest limitation: the **roster is current-only**. Discord cannot tell us who held
a role three months ago, so a count for a past period uses today's roster, and the output
says so.

The rules are deliberate and were agreed explicitly:

| Situation | Counted | Reason |
|---|---|---|
| `approved` | yes | normal case |
| still `pending` at count time | **yes**, reported separately | reviewers' backlog is not the reporter's fault |
| `approved_unknown` | **yes**, in its own category | a human approved it; only their role changed since |
| `rejected` | no | staff decided against it |
| `conflict` | no | guessing a side produces a wrong number |

`approved_unknown` is the fourth case and the subtle one. Discord only knows *current*
roles, so an approver who has since lost the role is indistinguishable from a stranger.
Their approval is neither a verified approval nor an absence of one. Reverting it to
`pending` would blame the reporter for someone else's promotion, and treating it as normal
would hide that the reviewer's authority is unverifiable, so it gets its own category:
counted, shown as «одобрено без прав», and listed in the header.

### Report presentation

- **Grouped by tier**, highest first, so the report reads like the hierarchy.
- **People are pings** (`<@id>`), not nicknames: a ping is how staff actually reach them.
- **A tier can be hidden per channel** (`/dsbot hide-tier`) without changing who is
  counted. Stored as `hidden_tiers` in the binding config.
- **One tier can be selected** (`tier:` on `/reports count`) to read a single group.
- A row shows its real total (`4/1`) with the unusual parts in brackets.
- A footer pings the developer, and only when `DSBOT_DEVELOPER_USER_ID` is set. Never a
  name or an external link: a report is visible to a whole server.

### Counting from Discord without getting rate limited

Reading *who reacted* is the expensive part: one request per emoji per message,
**rate limited per channel**. Discord buckets limits on the top-level resource in the path
and its docs say to honour the response headers rather than hardcode timings, so a month
of reports is thousands of requests and a 429 storm.

The fix is fewer requests, not slower ones, in three layers:

1. **No ✓/✗ on the message, no request.** The payload already carries the counts, so
   `_has_verdict_reactions` skips it for free.
2. **Unchanged counts, no request.** Same count means the same people reacted, so the
   stored reactor list is reused. Measured: 20 requests on the first scan, 0 on the second.
3. **Only new reports are fetched**, spaced by `DSBOT_REACTION_READ_INTERVAL`.

The cache is keyed on the **counts**, not the message id, so a new approval or a removal
invalidates it automatically. Do not "simplify" it to a message-id cache: stale entries
would freeze verdicts silently.

Never parallelise these reads. It looks correct locally because discord.py's bucket key
includes the message id, so it serialises per message — and then gets 429'd upstream,
costing more than the delay it saved. That mistake shipped once.

### Who is counted

Everyone holding a role in the **quota** ladder — including people with zero reports, who
must show as `0`. "Nobody reported" and "nobody is tracked" must never look the same.

This requires the **Members intent**: Discord refuses to list guild members without it.
Its absence is a configuration error to report, never a silent fallback. Roster
membership is scraped from the channel's **rank source guild** and stored in
`roster_members`, so counts are reproducible and a role change tomorrow cannot rewrite
last week.

The grain is the **person**, not the character: one instructor with several characters is
one workload.

### Period boundaries

ISO weeks, Monday 00:00 to the next Monday 00:00, in `DSBOT_DEFAULT_TIMEZONE`.
Half-open: `[start, end)`. A report filed Sunday 23:00 belongs to that week even if it is
approved on Monday, because credit follows the report, not the review.

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

### Strictness is deliberate

Report values feed weekly statistics, so silent tolerance produces wrong numbers. The
form is the specification; deviation is an error with a specific message.

Rejected on purpose: `[8076]` brackets, `01\0016` backslash, `NRP-`/`RP-` labels,
extra words in the formation, any capitalisation other than the template's, `;` as a
separator, and a second IDN shape that does not exist.

Kept: case-insensitive qualification codes with trailing commentary stripped, and
`formation | idn | idn` for every formation (only `01/` and `00/` are SOB-specific).

### Conditional rules must not guess

A rule keyed on the qualification does not fire when the qualification was not
recognised. Otherwise a reporter is told "need 2 photos" instead of "your qualification
is wrong", and the real mistake is hidden.

The one check that always applies is "at least one photo", because it is a property of
the form, not of the qualification.

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
8. **`create_all` does not alter existing tables.** Adding a column to a model left every
   existing database with the old shape, and the first query mentioning the column died
   with "no such column". The server database is the only copy of months of reports, so
   it cannot be rebuilt. `Database.prepare()` runs additive `ALTER TABLE ADD COLUMN`
   upgrades on every start; new columns need an entry in `storage/migrate.py`.
9. **A NULL capability must not mean "allowed".** Treating an unresolved rank as a
   reviewer would let any stranger's reaction approve a report. Null means "cannot
   review", and the report stays undecided.
10. **Republishing duplicated pages.** Editing only the first message of a multi-page
    report left the old later pages and added new ones. All published ids are stored and
    every page is edited, with orphans removed.
11. **One ladder was made to answer two questions.** Approving and owing a report were
    configured on the same tiers, so either everyone who reports could approve, or the
    people who owe reports fell out of the count. They are now separate ladders with a
    declared `purpose`, and the reactor path asks for `review` explicitly.
12. **Reaction events were deduplicated by content.** `react.add:...` carried no
    timestamp, so un-reacting and re-adding looked like a duplicate delivery and was
    dropped: the verdict never came back. Clears had the same bug and left stale
    reactions that counted as approvals. Both now carry the moment they happened.
13. **The weekly requirement was stored in the wrong place.** It lived in
    `quota_policies`, retyped per channel, while the ladder only listed roles. That made
    the ladder look like it had no numbers, forced the same figure to be entered again
    for every channel, and let a channel silently disagree with the ladder. The number is
    a property of the rank, so it is `required_per_week` on the tier, in the quota YAML,
    frozen onto the roster when scraped. `quota_policies` is now an override for the
    exception only.
14. **Counting the archive instead of the channel.** The database only holds what the bot
    saw while running, so "last week" returned 5 rows when 30 reports existed, and that is
    indistinguishable from "only 5 people reported". History is read from Discord now; the
    database keeps configuration, the roster, and the audit trail.
15. **A verification rule turned into a wrong number.** Requiring rank to prove an approval
    is right; treating an unprovable approval as "no approval" is not. Discord only knows
    *current* roles, so a reviewer who lost the role later made a valid report look
    unreviewed. It is `approved_unknown` now and still counts.
16. **Per-reaction rank lookups made a scan take half a minute.** One awaited REST call per
    reaction. Distinct reactors are resolved once each, concurrently.
17. **A row must not overfill its own quota.** `3/2` invited "out of what?". Display is
    clamped with a `+N` remainder.
18. **Parallelising reaction reads caused a 429 storm.** discord.py's bucket key includes
    the message id, so it believes every message has its own limit and does no
    cross-message throttling; Discord limits the route per *channel*. Eight at once looked
    fine locally and was slower in production, because each 429 costs seconds. The real
    fix is fewer requests: skip messages with no ✓/✗, and reuse stored reactors when the
    counts have not changed.

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
