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

Discord developer portal: enable **Message Content Intent**. **Server Members Intent is
not needed yet** - it becomes necessary only for rank resolution, and is toggled with
`DSBOT_ENABLE_MEMBERS_INTENT=true`.

### Bot permissions

Deliberately minimal:

| Permission | Why |
|---|---|
| View Channel | see the channel at all |
| Read Message History | read reports, backfill, scan |
| Send Messages | post warnings |
| Add Reactions | prepare for the verdict engine |

**Manage Messages is NOT required.** A bot may delete its own messages without it;
Discord only requires that permission to delete messages it did not send. Verified live
against a channel where `manage_messages` was `False` and self-delete succeeded.

`dsbot doctor` probes the real operations (send, self-delete, read history) instead of
trusting permission arithmetic, which is only accurate when the Members intent is on.

## Commands

```powershell
uv run dsbot init-db
uv run dsbot run                      # live bot; Ctrl+C to stop
uv run dsbot run --dry-run            # ingest + parse, never post

# channel setup
uv run dsbot bind --guild <guild> --channel <channel> --rank-guild <guild>

# per-channel forms (this is how a second channel gets its own format)
uv run dsbot form-export --channel <channel>     # -> forms/ch-<id>.json
uv run dsbot form-template --out forms/x.json    # blank starter to edit
uv run dsbot form-import --channel <channel> --file forms/ch-<id>.json
uv run dsbot parse --file sample.txt --channel <channel>

# history / analysis
uv run dsbot backfill --channel <channel> --since 14d --out backfill-out/reports.jsonl
uv run dsbot analyze --dump backfill-out/reports.jsonl
uv run dsbot export --channel <channel> --out backfill-out/reports.csv
```

`backfill --to-db` writes to the database in the same pass. `--since` accepts an
ISO date or a relative duration (`14d`, `36h`).

`analyze` is the important one before going live: it runs the *same* parser over a
real dump and reports match rate, which fields are missing, and sample messages the
schema does not yet cover.

## How per-channel forms work

A channel is bound to one or more **form schemas**, stored as JSON in the database and
selected per message by scoring. Adding a channel with a different format is adding a
JSON file — no code change:

```powershell
uv run dsbot form-export --channel <id>      # inspect what it currently uses
# edit the JSON: fields, labels, required-ness, enum options, typing
uv run dsbot form-import --channel <id> --file forms/ch-<id>.json
uv run dsbot parse --file example.txt --channel <id>   # verify before going live
```

Field `type` options: `text`, `long_text`, `enum`, `formation_idn`,
`discord_message_ref`, `attachment_required`, `count_fraction`, `name_ref`, `any`.

## What exists today

- `dsbot run` — live bot: ingests, parses, warns, clears warnings on fix
- `dsbot run --dry-run` — same pipeline, never posts
- per-channel form authoring: `form-export` / `form-template` / `form-import`
- `dsbot backfill` + `dsbot analyze` — measure the parser against real history
- `dsbot parse --channel <id>` — test a message against a channel's real form

Not built yet: verdicts from reactions, weekly quota evaluation, dashboards.

## Testing locally before touching the real server

1. Create a private test server and one channel in it.
2. `uv run dsbot init-db`
3. `uv run dsbot bind --guild <guild> --channel <channel>`
4. `uv run dsbot run`
5. Post a valid report, an incomplete report, and plain chat. Expect: no reply for
   chat, no immediate reply for a fresh incomplete report (a grace window), a warning
   for an old incomplete report, and the warning deleting itself when you edit the
   message to be complete.

`--dry-run` is the safe way to watch parsing decisions: it logs
`report_processed` with `matched`, `valid`, `action`, and issue codes without posting.

## Config: operator vs admin

Two different levels, on purpose:

| Level | Where | Changed by | Examples |
|---|---|---|---|
| Operator | `.env` | you, with a restart | grace window seconds, DB url, log level |
| Admin | database / form JSON | Discord admins | form fields, enum options, required-ness, quotas, rank ladders, message text |

The grace window (`DSBOT_REPORT_GROUP_WINDOW_SECONDS`) is **operator-only** by design:
it is deployment tuning, not per-channel policy.

## IDN rules

An IDN is **exactly 4 digits, or 6 or more**. Values are stored verbatim and never
zero-padded, so `001` stays `001` and is rejected as `idn_bad_length` rather than
silently becoming `0001`. Lore IDNs use `sub_unit/number`, e.g. `01/0016`.

## Stopping the bot

Closing the terminal, or pressing Ctrl+C in the wrong window, does **not** stop the bot:
`uv run dsbot run` spawns a child process that survives. Messages keep being answered
until that process dies. Use the commands instead:

```powershell
uv run dsbot status    # is it running, and which pid
uv run dsbot stop      # stop it cleanly (escalates to force)
```

`stop` signals politely, then terminates if needed, and confirms the process is gone.
A pid file in `var/dsbot.pid` makes this work no matter how the bot was started.

If `stop` ever fails, the pid is printed; terminate directly with
`Stop-Process -Id <pid> -Force`.

## Editing a channel's form (no code, no deploy)

Forms are YAML. This is the admin workflow:

```powershell
uv run dsbot form-pull  --channel <id>          # current form -> forms/ch-<id>.yaml
#   edit the YAML
uv run dsbot form-check --file forms/training.yaml    # validate, shows what will apply
uv run dsbot form-apply --channel <id> --file forms/training.yaml
```

`form-check` never touches anything; `form-apply` validates before writing, so a typo
cannot leave a channel unparseable. Changes take effect on the next message.

Where the form has rules, write them as `WHEN ... THEN ...`:

```yaml
rules:
  - id: photos_for_others
    field: photos
    when:
      - field: qualification
        op: ne          # eq / ne / in / not_in / gt / gte / lt / lte
        value: T
    min_attachments: 2
    message:
      ru: "Для этой квалификации нужно 2 и больше фото."
```

Rules exist because some requirements depend on *another* field: "1 photo if T,
otherwise 2+" cannot be expressed on the photos field alone.

## Formations

```yaml
formations: [SOB, ARC, CT, 501ST, 212TH, GUARD, GHOST, JEDI, 104TH, MERC]
```

Reporters always write a formation. Squads (ALPHA, NULL, ...) are internal structure
and are **not** part of the form: `ALPHA | 01/0016` is rejected as an unknown formation,
and a soldier writes `SOB | 0016` or `SOB | 01/0016`.

Exactly **two** IDN shapes exist, and nothing else is accepted:

| Shape | Example | Notes |
|---|---|---|
| plain, 4 digits | `SOB \| 0016`, `ARC \| 0042` | any formation |
| plain + second IDN | `ARC \| 0042 \| 0043` | any formation; both exactly 4 digits |
| prefixed, 6 digits `PP/00NN` | `SOB \| 01/0016`, `SOB \| 00/0087` | **SOB only** |
| prefixed + system IDN | `SOB \| 01/0016 \| 0017` | system IDN is exactly 4 digits |
| several soldiers | `ARC \| 7784, ARC \| 8440` | comma-separated, each checked |

- The prefix is always `01` or `00`, and the two digits after the slash are always `00`.
- `01/` and `00/` describe SOB squads, so any other formation using them is rejected.
- A second 4-digit IDN is fine for **any** formation; only the `01/` `00/` prefixes are
  SOB-specific.
- A prefixed IDN always needs its system IDN.

Rejected:

| Input | Reason |
|---|---|
| `SOB \| 000016` | `idn_bad_shape` — no plain 6-digit form |
| `SOB \| 01/0116` | `idn_bad_shape` — after the slash must start with `00` |
| `ARC \| 01/0016` | `lore_prefix_not_allowed_for_formation` |
| `SOB \| 01/0038` | `nrp_missing` — prefixed IDN needs its system IDN |
| `SOB \| 02/0016` | `lore_prefix_invalid` |
| `SOB \| 016` | `idn_bad_shape` |
| `ARC \| 0042 \| 043` | `nrp_bad_length` — second IDN must be 4 digits |

Prefixes and which formations may use them are both configurable per field
(`lore_prefixes`, `lore_prefix_formations`), defaulting to `["01","00"]` and `["SOB"]`.

The two positions have different rules on purpose, and their warnings say so:
`idn_bad_length` means a lore IDN is the wrong length (4 or 6+), while
`nrp_bad_length` means the system IDN is not exactly 4 digits.

## Checking the form against real traffic

`scan` reads recent messages and prints a verdict for each one. It is strictly
read-only: it never reads reactions, never writes to the database, and never posts.

```powershell
uv run dsbot scan --channel <id> --limit 100
uv run dsbot scan --channel <id> --limit 100 --out backfill-out/scan.txt
```

Output is one block per message:

```
  1. [INVALID     ] 11-20 12:04  instructor
       text: 1. SOB | 0016 / 2. INF / 3. / 4.
       parsed: identity=SOB | 0016  |  qualification=INF  |  photos=1 photo(s)
       - Фото: нужно не меньше 2, найдено 1.
       - Нужна ссылка на предыдущий одобренный рапорт (кроме квалификации T).
```

Markers are `VALID`, `INVALID`, `not a report`, and `photo`. This is the fastest way to
see whether a form definition matches reality before trusting it live.

Note: read the file with a UTF-8 aware editor, or `Get-Content -Encoding UTF8` in
PowerShell; the default Windows console codepage will mangle Cyrillic.

## Adding a channel in another server

The bot must be invited to that server first, then one command binds the channel and
applies the form:

```powershell
uv run dsbot form-apply --channel <channel-id> --file forms/training.yaml --guild <guild-id>
```

Because the bot needs to be in that guild, the invite URL is:

```
https://discord.com/oauth2/authorize?client_id=1573038712055537917&permissions=68672&scope=bot
```

`dsbot doctor` prints the same URL if the bot cannot see a guild.

Copying an existing form to a new channel:

```powershell
uv run dsbot form-pull  --channel <source-id> --out forms/copy.yaml
uv run dsbot form-apply --channel <target-id> --file forms/copy.yaml --guild <target-guild-id>
```

`dsbot channels` lists every bound channel with its server, module, and form, which is
the quickest way to confirm setup:

```
             channel                server  module   form                 locale
 1573045809774067812   1209516013779943544  reports  training_report      ru   [Сервер Foggytown]
```

Each channel is independent: the same form in two channels shares one schema row, while
changing one channel's form never affects the other.

## Что видит нарушитель

```
<@42>, в рапорте есть ошибки формы:

- Неизвестное формирование: «99».
- Допустимые формирования: 104th, 212th, 501st, 99th, ARC, CT, Ghost, Guard, Jedi, Merc, SOB.
- Неизвестная квалификация: «pepe».
- Допустимые квалификации: T, INF, M-A-C, M-L-C, M-U-C, D-E-C, D-S-C, D-U-C.

Правильный вид — в закреплённом сообщении.
Исправьте сообщение — это предупреждение исчезнет само.
Если бот ошибается — напишите <@263344369451401227>.
```

Принципы:

- пинг автора обычный, без ника; ник показывается только в логах скана;
- правильный вид НЕ дублируется — отсылка к закреплённому сообщению;
- контакт задаётся в `.env` через `DSBOT_CONTACT_USER_ID`;
- условные правила (фото, ссылка) **не срабатывают**, если квалификация не распознана:
  иначе человек получает «нужно 2 фото» вместо настоящей ошибки;
- отсутствующее поле сообщается один раз, без дубля «найдено 0».

## Разбор реальных отчётов

Формат терпим к тому, как люди пишут на самом деле:

| Написание | Результат |
|---|---|
| `SOB \| 01/0038 \| 8289` | второй IDN через `\|` — верно |
| `SOB \| 01\0095` | обратный слеш тоже принимается |
| `SOB \| NRP-4113 \| RP-1262 \| RC-62` | метки NRP/RP/RC снимаются |
| `ARC \| (8377) 01\0016` | числа в любом порядке |
| `ARC \| 7784 ; ARC \| 8440` | несколько бойцов в одном рапорте |
| `ARC 0085` | без разделителя тоже читается |
| `СT`, `CT`, `212th`, `212TH` | регистр и кириллические похожие буквы |
| `D-U-C (Инструкторская система...)` | пояснение после кода отбрасывается |

Отклоняется намеренно, потому что шаблон другой:

| Написание | Почему |
|---|---|
| `SOB \| 01/0038 [8289]` | `brackets_not_allowed` — разделитель `\|`, а не скобки |
| `99 \| 9902` | `formation_unknown` + список допустимых |
| `501th` | не `501st` |
| `ARC \| 01/0016` | префикс только у SOB |
| `212th \| Revers` | в поле IDN имя вместо номера |

## Поле 1: строгая форма

Поле 1 может содержать несколько бойцов через запятую, и каждый проверяется отдельно:

```
ARC | 0042
SOB | 01/0016 | 0017
ARC | 7784, ARC | 8440
```

Разрешено ровно две формы IDN: обычный 4-значный и лорный `01/00NN` или `00/00NN` вместе
с системным IDN. Всё остальное — ошибка с указанием, что именно исправить:

| Написание | Почему отклонено |
|---|---|
| `ARC \| 01/0016` | префикс только у SOB |
| `SOB \| 01/0038` | к лорному IDN нужен системный IDN |
| `ARC \| 01/0016` | префикс `01/` и `00/` только у SOB |
| `SOB \| 01/0038 [8289]` | скобки не нужны |
| `ARC Alpha \| 01/0016` | лишнее слово в формировании |
| `212th \| Revers` | имя вместо номера |
| `99 \| 9902` | неизвестное формирование |
| `ARC 0085` | нет разделителя `\|` |

Терпимость оставлена только к регистру и кириллическим похожим буквам: `212th`,
`212TH` и `СT` (кириллица) — это одно и то же формирование.

## Временно убрать форму с канала

```powershell
uv run dsbot disable --channel <id>   # канал перестаёт обслуживаться
uv run dsbot enable  --channel <id>   # вернуть как было
uv run dsbot channels                 # видно состояние: active / PAUSED
```

`disable` сохраняет привязку, форму и всю историю. Это правильный способ «выключить
канал на время»: `enable` возвращает всё одной командой, переобъявлять форму не нужно.

Действует сразу, даже на запущенном боте — привязка читается на каждом сообщении.

Важно: `form-apply` **не** включает приостановленный канал. Если канал на паузе,
применение формы обновит форму, но оставит канал выключенным.

`dsbot channels` показывает оба состояния:

```
             channel                server  module   form                 locale state
 1274460260941434901   1274460259661910149  reports  training_report      ru     active   [INS | ROTR]
 1573045809774067812   1209516013779943544  reports  training_report      ru     PAUSED   [Сервер Foggytown]
```

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
