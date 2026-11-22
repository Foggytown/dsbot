# AGENTS.md

Conventions and settled decisions for this repository. Read before changing architecture.

## What this project is

A Discord bot that passively monitors report channels in an RP military community,
parses reports written as numbered forms, tracks staff verdicts expressed as reactions,
and evaluates weekly quotas per character.

Deployed phases:

- Phase 1 (done): capture -> parse -> validate -> warnings -> export.
- **Phase 2 (done):** cross-server rank resolution, verdict engine from reactions.
- **Phase 3 (done):** counting reports per person for any period, read from Discord
  history; roster and quota ladders; slash commands; CSV and optional publication.
- **Next (not started):** scheduled weekly posting. Possible later: ingesting reactions
  as they happen so recent scans need no API calls; dashboards.

**Where things stand.** Phase 3 is deployed and working on the real server: both channels
are bound, the roster is scraped, and `/reports count` produces numbers. Phase 4 - the
periodic report - is implemented: a channel stores its own close time via `/results set`,
and the bot publishes the finished period into the channel named by `/dsbot report-target`.
Open decisions are listed at the end of this file under "Open questions" — read them before
starting new work; several are choices that belong to the community, not to code.

**The panel's settings are a web form, not a `.env` file.** Values persist across uploads,
so a stale entry keeps applying until somebody deletes it by hand. Keep it minimal — token
and `DATA_DIR` — and see "The panel stores settings as a form" below.

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

### The panel stores settings as a form, not a file

On a hosting panel there is no `.env` to edit: the variables are a web form, and they
persist across uploads. A value left over from an older version therefore keeps being
applied until it is deleted there by hand.

That is how a stale `DSBOT_DATABASE_URL=./var/dsbot.db` moved the database out of `/app/data`
and into the checkout. The bot now *ignores* a relative sqlite path, uses the default
inside the data directory, and prints what it did. Prefer the panel's form over a `.env`
file, and keep it minimal: token plus `DATA_DIR`, nothing else unless needed.

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

The optional system (NRP) IDN is exactly 4 digits. It may follow **either** form:
`SOB | 0016 | 0017` and `SOB | 01/0016 | 0017` are both valid - some soldiers have both a
lore IDN and a system IDN. A bare 6-digit `SOB | 000016` is still invalid; the two IDNs
are separate fields, not one long number.

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
not be resolved is stored but counts for nothing - a NULL `can_review` must never be
treated as "allowed", or any stranger's check mark would approve a report.

**`approved_unknown` is a deliberate compromise, not a safeguard.** When the only check
marks come from reactors whose rank cannot be resolved, the report becomes
`approved_unknown` and **is credited** toward the quota. The reasoning: a human did approve
it, and the common cause is that they held the role at the time and have since lost it.
The cost is real and accepted: a check mark from somebody who never had the right is
indistinguishable from one by a reviewer whose role was removed, so in principle a friend's
reaction can close somebody's weekly norm.

This was raised in review as a possible hole and **closed as "leave it"**. Do not "fix" it
by making `approved_unknown` uncounted without asking: that would silently uncredit every
genuinely-approved report whose reviewer has since been promoted or left. If it ever needs
tightening, the correct change is to store `can_review=False` when a rank *was* resolved but
carries no review capability, so "known and unauthorised" separates from "unknown" - and
that is a decision, not a clean-up.

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
| `conflict` | no, counted as declined | same-tier disagreement; logged, never announced |

`approved_unknown` is the fourth case and the subtle one. Discord only knows *current*
roles, so an approver who has since lost the role is indistinguishable from a stranger.
Their approval is neither a verified approval nor an absence of one. Reverting it to
`pending` would blame the reporter for someone else's promotion, and treating it as normal
would hide that the reviewer's authority is unverifiable, so it gets its own category:
counted, shown as «без роли», and listed in the header.

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

**A formation must match the configured spelling exactly.** Not upper-cased, not
case-folded: `SOB` and `501st` and `Guard` are accepted because that is how they are
written in `forms/training.yaml`, while `sob`, `Sob`, `501ST` and `GUARD` are rejected as
`formation_unknown`. The list is the specification for how the channel writes its formations,
so an admin adding `501ST` alongside `501st` would create two formations that look identical
to a human.

### Conditional rules must not guess

A rule keyed on the qualification does not fire when the qualification was not
recognised. Otherwise a reporter is told "need 2 photos" instead of "your qualification
is wrong", and the real mistake is hidden.

The one check that always applies is "at least one photo", because it is a property of
the form, not of the qualification.

### The link is checked for presence, not for approval

Field 4 must contain a Discord message link that points at a report giving the right to a
qualification promotion. The bot checks only that **a Discord link is present**, plus that
it is in the right numbered field.

It deliberately does **not** fetch the linked message to confirm that report was approved.
That was decided explicitly: the approval of the linked report is a human judgement made by
senior instructors while approving this one. Enforcing it in code would mean an extra
Discord request per report and a rule for what "approved" means in another channel, to
reproduce a check somebody is already doing.

So the warning text states the requirement ("ссылка на одобренный рапорт, дающий право на
повышение квалификации") while the code only verifies presence. That gap is intentional.
Do not "fix" it into a fetch without asking first.

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
- **Scripts, not assertions, for anything involving cost or persistence.** Claims like
  "this is fast" or "this survives a redeploy" are proven by a `scripts/e2e-*.py` that
  measures or exercises it. Several bugs in this project were invisible to unit tests and
  obvious to a 20-line script.
- Time-dependent tests must not depend on the current date. Use an explicit `now=`.

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
17. **A row must not overfill its own quota - but the number is not capped.** The first
    attempt clamped `4/1` to `1/1 +3`, which made the reader add numbers up and hid the
    real total. The row now shows the true `4/1`, because "4 out of 1" says exactly what
    happened. Anything unusual goes in brackets afterwards.
18. **Parallelising reaction reads caused a 429 storm.** discord.py's bucket key includes
    the message id, so it believes every message has its own limit and does no
    cross-message throttling; Discord limits the route per *channel*. Eight at once looked
    fine locally and was slower in production, because each 429 costs seconds. The real
    fix is fewer requests: skip messages with no ✓/✗, and reuse stored reactors when the
    counts have not changed.
19. **A cache keyed on counts cannot see a swap.** One person removing a check while
    another adds one leaves the count identical while the *people* change - and who
    reacted is what decides authority. Freshness is therefore judged by period: a closed
    period is historical fact and is reused freely, a recent message only for two minutes.
20. **The reaction cache wrote into the live verdict table.** A scan used
    message_reactions to remember what it had fetched, and replaced rows by message id.
    That table is the gateway's own record, carrying the rank snapshot that decides who had
    authority - so the first scan of a live-report message deleted a real approval's
    provenance and replaced it with rows having can_review = NULL. The cache now lives in
    its own `reaction_cache` table. Any new cache must never overwrite observed state.
21. **Startup only applied the channels file when a setting was truthy.** The guard tested
    `settings.channels_file`, which is deliberately empty because the real path is derived
    from the data directory - so the condition was False on a fresh panel, no channel was
    ever bound, and the bot started successfully while serving nothing. Never gate config
    reconciliation on a setting whose default means "derive it"; reconcile unconditionally.
22. **A database path outside the data directory was accepted silently.** The panel replaces
    everything except its data directory, so an explicit `DSBOT_DATABASE_URL` pointing into
    the checkout deletes all history on the next upload. A *relative* sqlite path is now
    **ignored**: the default location inside the data directory is used and the reason is
    printed once at startup. Not fatal, because the panel's settings are a web form the
    user edits, so refusing to start would leave the bot down until they noticed.
23. **Slash commands were only synced to guilds with a binding.** A guild could keep a stale
    registration from an earlier deploy, so Discord kept offering a command this process
    could not resolve and every use logged
    `CommandNotFound: Application command 'reports' not found`. Sync every guild the bot is
    in; the cost is one request per guild, the alternative is a permanently stale menu.
24. **Personal details in a committed script.** A preview script carried the author's
    username and a repository URL, which would have shipped in every deployment. The
    developer credit is a Discord ping only, read from settings; nothing name-like is
    hardcoded, and never add a name or external link to user-visible output.
25. **A `MESSAGE_UPDATE` is not an edit.** After a deploy the bot warned about seven reports
    from 2025 (up to 414 days old) in the space of six seconds. The trigger was
    `on_raw_message_edit`, which treated *every* `MESSAGE_UPDATE` gateway event as "the
    author edited this message" and re-ran the whole parse-and-warn pipeline. Discord sends
    that event for far more than a text edit - an embed unfurl, a pin, a link preview.
    The captured payloads show the discriminator precisely: the bogus events carried
    `edited_at: null`, and they carried the *current* content, so a content comparison
    alone cannot recognise them (the text already matched the database). `edited_at` is set
    only when a person edits, so `_edit_changed_message` checks it first. Never infer
    "the author changed this" from the event name; check the field the author controls.
26. **Nothing bounded the age of a warning.** The same incident showed the warning path had
    no notion of report age: a report from March 2025 was treated exactly like one posted
    seconds earlier, because reasons to re-evaluate (a reaction, a phantom edit, a replay)
    say nothing about whether a report is late. Warnings now have a ceiling -
    `DSBOT_REPORT_WARN_MAX_AGE_HOURS`, default 24 h, a **default rather than an opt-in**,
    because live bot, replay and backfill all re-evaluate old messages and a forgotten
    argument would restore the bug. `0` disables it for a deliberate backfill. An old
    report is still stored as `invalid`; only the reply is suppressed, since a silent
    channel must not mean a silent archive.
27. **A log line that names only the bot's own message.** `warning_posted` carried
    `message_id` - the warning the bot had just posted - so every line in that incident
    pointed at a bot message and the report could only be recovered with a database query.
    Warnings now log and store `report_message_id` as well. Any log about our own output
    must also name the input that caused it.
28. **The diagnosis was provable from the database, and was proved.** `message_id` fields
    in a log are Discord snowflakes, so `/ (id >> 22) + 1420070400000` dates any of them
    with no API call: the ids in the incident log decoded to the bot's own warning times.
    `scripts/audit-warnings.py` then answered the whole question read-only, joining
    `validation_messages -> reports -> messages` and classifying each trigger from
    `raw_events` ("ONLY seen as an EDIT"). Two habits that paid off: make the audit tool
    refuse to lie about a stale file, and never write to a database pulled off the server.
29. **A setting could only be made in the channel it did not apply to.** `/dsbot
    report-target` wrote the destination onto the binding of the channel the command was
    typed in, and a *destination* channel is not bound to a form. So typing it where it
    makes sense raised `ValueError`, the interaction failed, and the user saw nothing -
    the feature was reported as "doesn't work" and looked like a Discord problem. Any
    command that configures one channel while being typed in another must resolve the
    binding by intent, and must turn every failure into a visible sentence.
30. **A schedule is a stored instant, not a timer.** Phase 4 deliberately does not use
    APScheduler and does not build timers at startup. The close time is one editable field
    per channel, re-read every minute, so an admin changing it from Discord takes effect
    without a restart and a timer cannot drift from the database. Publishing happens
    **before** the schedule advances: a failed publish must leave the period due, or that
    week is silently lost.
31. **A count was thrown away and silently replaced by a different one.** `publish_period`
    accepted the Discord count as a parameter named `counted` and then never used it,
    calling `CountingService.count()` instead - which reads the database. The published
    weekly report therefore showed every report as unreviewed, because the archive only
    holds reactions the bot saw while running, while `/reports count` showed the real
    verdicts. Both numbers were plausible, which is what made it hard to see. Having
    counted once, publish that. If two paths can produce the same table, they must share
    one result object, not re-derive it.
32. **A parameter that is accepted and unused is worse than a missing one.** The bug above
    was invisible to every test and to the type checker: `counted: Any` matched, the call
    succeeded, and only a live comparison of the two commands' output exposed it. When a
    function takes a prepared result, either use it or do not accept it - and prefer a
    test that asserts the two entry points agree.
33. **A link in the wrong numbered field was silently accepted.** Field 3 is the photos
    field and accepts text, so a previous-report link pasted there (instead of field 4) was
    stored without complaint; the promotion link was simply absent. With photos also
    attached the report was judged **valid**, so the reporter was never told and the
    evidence chain was broken. `link_in_wrong_field` now names both field numbers and tells
    the reporter to move it. The general lesson: a field that "accepts text" will swallow
    anything, so validate what the text *is*, not merely that something is there.
34. **The age limit froze existing warnings instead of only preventing new ones.** The
    guard meant "do not complain about an ancient report" was applied to the whole warning
    path, so an existing warning on an old report was never edited and never cleared. A
    reporter who moved the link into field 4 saw the same complaint forever, which is the
    "edit it and it clears itself" promise broken. The guard now applies only when there is
    no warning row yet (`has_warning`); a warning that exists is always maintained. Two
    separate questions: *may I start nagging*, and *may I update what I am already saying*.
35. **Evidence text with no attachment was only a warning.** `3. фото` with zero files
    attached was reported as a warning, and warnings do not invalidate a report - so a
    report with no evidence at all validated as **valid** and counted toward a quota. It is
    an error now. A field that names evidence is asserting a file exists; when it does not,
    nothing about the report is correct.
36. **A warning that says «evidence_2» is not a warning.** With no label on the evidence
    fields, the fallback printed the parser's internal id, so the channel saw
    "В поле «evidence_2» текст вместо фото". The hint now uses the reporter's own numbered
    path ("пункт 3"), because that is what they are looking at. Never print an internal
    identifier to a user.
37. **A link inside a sub-item was invisible.** Reporters write evidence as `3.
    Доказательства:` followed by `3.1. <link>`. Sub-item text is a separate token and is
    not part of the parent field's value, so a check on the bound field alone never saw the
    link - and the report validated as correct with the promotion link missing. The check
    now scans the tokenized sub-items too. Whichever structure a field is *written* in, the
    check must look at the text, not at the parsed value.
38. **The edit guard read a field name Discord never sends.** `on_raw_message_edit` receives
    the raw gateway payload, where a real edit is marked by `edited_timestamp`.
    `edited_at` is what our own MessageSnapshot dataclass calls it, and the guard checked
    that name against the raw dict - so it was always None, every genuine edit was treated
    as a phantom server-side update, and no warning ever updated after an author fixed
    their report. Every test passed, because the tests were written from the same wrong
    assumption. Lesson: when reading a third-party wire format, assert the key against that
    format's documented name, never against our own model's field names.
39. **A diagnostic line proved the wrong thing.** The report of "the warning never clears"
    came with a `report_processed ... action=warned` line, which is emitted on *creation*.
    An edit emits `report_reparsed_after_edit`. Confusing the two sent the investigation
    after the parser twice before pointing at the handler that never ran. When a bug is
    "nothing happened", find the log line that *would* have been written and establish that
    it is absent - do not reason from a line that describes a different event.

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

## Phase 3 in one page

Four moving parts, and knowing which owns what prevents most mistakes.

| Piece | Lives in | Answers |
|---|---|---|
| review ladder | `forms/ladders/review.yaml` | who may approve, and who outranks whom |
| quota ladder | `forms/ladders/quota.yaml` | who owes reports, and how many per week |
| roster | `roster_members` table | who was on those roles when the roster was scraped |
| counting | `modules/quotas/` | how many reports each person filed in a period |

**Flow when someone runs `/reports count`:**

1. the period is resolved (`period.py`) — any dates, or a named week
2. the channel's binding supplies the form, the ladders, and the hidden tiers
3. `live.py` reads the channel's history for that window (not the archive)
4. messages are grouped into reports, parsed, and filtered by tier
5. reactors are read from Discord, or reused from the cache when provably unchanged
6. the verdict engine computes approved / rejected / conflict / approved-unknown
7. `counting.py` tallies per person, `render.py` groups by tier and renders

**Design decisions worth not re-litigating:**

- History comes from Discord; the database holds configuration, the roster, and audit.
- An unreviewed report still counts as pending; only a rejection or conflict removes it.
- `approved_unknown` exists because Discord only knows *current* roles.
- A quota ladder cannot carry `capabilities`; a review ladder cannot carry quotas.
- Rows are grouped by tier, people are pings, and the footer is a ping only.

**Scripts that prove it still works** (run the relevant one after touching this area):

```
scripts/e2e-counting.py        count a fixture period end to end
scripts/e2e-live.py           read from Discord-shaped history, group photos
scripts/e2e-quota.py          highest quota tier wins, review roles owe nothing
scripts/e2e-ladders.py        review and quota ladders stay separate
scripts/e2e-deploy.py         a redeploy rebuilds config and keeps the roster
scripts/e2e-channels-file.py  channels.yaml lives in /data and survives deploys
scripts/e2e-settings.py       Discord-side settings survive restart and sync
scripts/e2e-reaction-cache.py cached reactors reused, changes refetch
scripts/check-commands.py     the slash command tree is valid
scripts/preview-report.py     print the exact message staff would see
scripts/profile-*.py          where the time goes, measured
scripts/e2e-conflict.py       conflict stays silent and credits nobody
scripts/e2e-ladder-log.py     the review-role diagnostic counts correctly
scripts/e2e-roster-discord.py /dsbot roster-refresh populates the roster
scripts/e2e-warning-age.py    an old report is stored but never warned about
scripts/repro-stale-warning.py fixing part of an old report updates its warning
scripts/e2e-phantom-edit.py   real MESSAGE_UPDATE payloads: phantom edits skipped
scripts/e2e-weekly-schedule.py set a schedule, close it, publish, roll forward
scripts/e2e-run-matches-count.py run and count render the same table
scripts/audit-warnings.py     read-only: which reports were warned about, and why
scripts/e2e-panel-start.py    panel startup: bindings appear, bad DB URL ignored
scripts/show-commands.py      print every slash command and its parameters
```

## Phase 4 in one page

The periodic report. Four moving parts:

| Piece | Lives in | Answers |
|---|---|---|
| the schedule | `channel_bindings.config` (`next_close`, `period_days`) | when does this channel's period end |
| `/results set` | `discord/results_commands.py` | the admin's one command to configure it |
| the watcher | `bot.py::_watch_close_schedule` | has a close time passed |
| the close | `modules/quotas/close.py` | count, publish, then advance |

**The admin's model is one number.** `/results set period:week next_result:22.11.2026
17:00:00` stores "the period ending on the 22nd is a week long". Two different things,
deliberately separated:

| The close... | decides |
|---|---|
| **time** (`17:00`) | *when* the report is published |
| **date** (`22.11`) | *what* is counted: whole local days, `16.11–22.11` |

So 17:00 and 23:59:59 on the 22nd report exactly the same week. Tying the window to the
clock time instead made a 17:00 close report `15.11–22.11` - eight calendar days, correct
arithmetic for "exactly 7×24 hours" and not what anybody means by "the week".
`Schedule.period_for(timezone)` is the only way to ask for the window, because "whole
days" needs a timezone.

**Why polling and not APScheduler.** The close time is editable from Discord at any
moment; a timer computed at startup would be wrong the instant somebody changes it. One
read per minute cannot drift from the database. APScheduler stays in the dependencies but
is not used for this.

**The order of operations is the design.** Count, publish, *then* advance. If publishing
fails the schedule does not move, so the period stays due and is retried on the next tick.
Advancing first would mark a week as done while nothing was posted.

**The published table is the Discord count.** `close_period` counts from history, and
`publish_period` publishes *that* result. It does not re-count from the database: the
archive only knows reactions the bot saw live, so a database recount showed every report as
unreviewed. A scheduled run and `/reports count` must render the same numbers, and
`scripts/e2e-run-matches-count.py` asserts it.

**A missed close is skipped, not caught up.** Decided explicitly: a bot that was down for
three days must not dump a stale week into the channel on restart. It logs the skip,
advances, and the admin can publish it by hand with `/reports count`. The one exception is a
grace window (`DEFAULT_GRACE`, two hours) so that a redeploy which lands a minute after the
boundary does not cost a week - that case is a restart, not an absence.

**A re-run edits, never duplicates.** `weekly_runs.published_ids` holds every page's
message id, so running the same period again edits those messages. This is why the run is
persisted *before* publishing.

## Open questions — decisions, not bugs

These were deliberately left to the community. Do not invent an answer to them.

1. **Reaction ingestion, catching up on old reviews.** Live ingestion already exists:
   `on_raw_reaction_add` stores each reaction with its rank snapshot, so reviews that
   happen while the bot runs cost nothing later. The gap is history — reviews that
   happened while it was not running — which are read from Discord on demand at ~1 request
   per reviewed report. Whether a scan should *store* what it fetches (so the bot keeps
   its own evidence rather than only a cache) is undecided.
2. **Jedi identity.** JEDI reports identify soldiers by name, not IDN. No such reports
   arrive today, so the IDN field is correct for now. Do not force a name into an IDN.
3. ~~Conflict notification.~~ **Settled: never announce.** A same-tier disagreement is
   silent — logged only, never posted. The report is recorded as `conflict`, counted as
   declined, and the weekly run proceeds without channel noise. An automatic run must be
   able to finish unattended, so nothing about a conflict can require a human in-channel.
4. **Reports with form errors.** They currently count toward quotas if approved, on the
   grounds that a human approved them. Whether a report that was never fixed should count
   is a policy question.
5. **Ladders and forms stay in git.** Channels moved to `/data` because they change
   operationally; ladders and forms are reviewed, so they still need a push. Making them
   editable from Discord is future work.
6. ~~Historical rosters.~~ **Settled: not wanted.** A count for a past period uses
   today's roster, and that is fine — roles change rarely and keeping membership history is
   complexity nobody asked for. Do not build it.
