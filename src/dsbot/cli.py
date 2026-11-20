"""Command-line entry point.

Kept deliberately thin: every command is a few lines over a module function, so the
same logic is callable from tests and from the Discord bot later.

Typical first run:
    dsbot init-db
    dsbot bind --guild <id> --channel <id> --module reports --schema training_report
    dsbot backfill --channel <id> --since 14d --out backfill-out/channel.jsonl
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import discord
import structlog
import typer

from dsbot.config import Settings, get_settings
from dsbot.logging_setup import configure_logging
from dsbot.storage.db import Database

log = structlog.get_logger(__name__)

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="dsbot - Discord report monitoring and aggregation.",
)


class ModuleKey(StrEnum):
    reports = "reports"


def _settings(verbose: bool = False) -> Settings:
    settings = get_settings()
    configure_logging("DEBUG" if verbose else settings.log_level, settings.log_format)
    return settings


@app.command("init-db")
def init_db(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    """Create all tables. Idempotent; safe to re-run."""
    settings = _settings(verbose)

    async def run() -> None:
        db = Database(settings)
        try:
            await db.create_all()
            ok = await db.healthcheck()
            typer.echo(f"database ready: {settings.database_url}")
            typer.echo(f"healthcheck: {'ok' if ok else 'FAILED'}")
        finally:
            await db.dispose()

    asyncio.run(run())


@app.command("bind")
def bind(
    guild: Annotated[int, typer.Option(help="Report guild id")],
    channel: Annotated[int, typer.Option(help="Channel id to bind")],
    module: Annotated[ModuleKey, typer.Option(help="Module key")] = ModuleKey.reports,
    schema_key: Annotated[str, typer.Option(help="Form schema key")] = "training_report",
    rank_guild: Annotated[
        int | None, typer.Option(help="Guild whose role ladder governs this channel")
    ] = None,
    locale: Annotated[str, typer.Option(help="Locale for bot messages")] = "ru",
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Bind a channel to a module and a form schema."""
    settings = _settings(verbose)

    async def run() -> None:
        from dsbot.modules.reports.schema import DEFAULT_SCHEMAS
        from dsbot.storage import admin

        db = Database(settings)
        try:
            async with db.write_session() as session:
                await admin.ensure_schema_seed(session, DEFAULT_SCHEMAS)
                binding = await admin.bind_channel(
                    session,
                    guild_id=guild,
                    channel_id=channel,
                    module_key=module.value,
                    schema_key=schema_key,
                    rank_source_guild_id=rank_guild,
                    locale=locale,
                )
                typer.echo(f"bound channel {channel} -> binding {binding.id}")
        finally:
            await db.dispose()

    asyncio.run(run())


@app.command("backfill")
def backfill(
    channel: Annotated[int, typer.Option(help="Channel id to export")],
    since: Annotated[
        str | None,
        typer.Option(help="ISO date/datetime, or relative like 14d / 36h"),
    ] = None,
    limit: Annotated[int | None, typer.Option(help="Max messages")] = None,
    out: Annotated[Path | None, typer.Option(help="JSONL output path")] = None,
    to_db: Annotated[bool, typer.Option("--to-db", help="Also write to the database")] = False,
    reactions: Annotated[
        bool, typer.Option("--reactions/--no-reactions", help="Fetch reactor lists")
    ] = True,
    include_bots: Annotated[bool, typer.Option("--include-bots")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Export channel history to JSONL and/or the database."""
    settings = _settings(verbose)
    if out is None and not to_db:
        out = Path("backfill-out") / f"channel-{channel}.jsonl"

    from dsbot.jobs.backfill import JsonlWriter, backfill_channel, parse_since

    async def run() -> None:
        since_dt = parse_since(since)
        db = Database(settings) if to_db else None
        client = discord.Client(intents=discord.Intents.none())
        runner: asyncio.Task[None] | None = None

        try:
            # A gateway session is started so history() has an authenticated HTTP
            # session. No intents are requested: backfill only reads history.
            runner = asyncio.create_task(client.start(settings.discord_token))
            await _wait_until_ready(client, deadline_seconds=60.0, runner=runner)

            ch = client.get_channel(channel)
            if ch is None:
                ch = await client.fetch_channel(channel)
            if not isinstance(ch, discord.abc.Messageable):
                raise typer.BadParameter(f"channel {channel} is not a text channel")

            writer = JsonlWriter(out) if out is not None else None
            try:
                if writer is not None:
                    writer.__enter__()
                stats = await backfill_channel(
                    ch,
                    since=since_dt,
                    limit=limit,
                    writer=writer,
                    db=db,
                    include_reactions=reactions,
                    include_bots=include_bots,
                )
            finally:
                if writer is not None:
                    writer.__exit__()

            typer.echo(
                f"messages={stats.messages} reactions={stats.reactions} "
                f"skipped_bots={stats.skipped_bots} errors={stats.errors}"
            )
            if out is not None:
                typer.echo(f"wrote {out}")
        finally:
            if db is not None:
                await db.dispose()
            if not client.is_closed():
                await client.close()
            if runner is not None:
                runner.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await runner

    asyncio.run(run())


async def _wait_until_ready(
    client: discord.Client,
    *,
    deadline_seconds: float,
    runner: asyncio.Task[None] | None = None,
) -> None:
    """Poll for READY. discord.py exposes no awaitable equivalent on Client.

    If the start task fails (bad token, unknown intent, network), that exception is
    re-raised instead of being masked by a generic timeout. A wrong token must say so.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + deadline_seconds
    while not client.is_ready():
        if runner is not None and runner.done():
            exc = runner.exception()
            if exc is not None:
                raise exc
            raise RuntimeError("discord client stopped before becoming ready")
        if client.is_closed():
            raise RuntimeError("discord client closed before becoming ready")
        if loop.time() > deadline:
            raise RuntimeError(
                f"discord client did not become ready within {deadline_seconds:.0f}s"
            )
        await asyncio.sleep(0.25)


@app.command("export")
def export(
    channel: Annotated[int, typer.Option(help="Channel id")],
    since: Annotated[str | None, typer.Option()] = None,
    out: Annotated[Path, typer.Option(help="CSV output path")] = Path("backfill-out/export.csv"),
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Export stored reports for a channel as CSV."""
    settings = _settings(verbose)

    from dsbot.jobs.backfill import parse_since
    from dsbot.modules.export.csv_export import export_reports_csv

    async def run() -> None:
        db = Database(settings)
        try:
            count = await export_reports_csv(
                db, channel_id=channel, since=parse_since(since), out=out
            )
            typer.echo(f"wrote {count} reports to {out}")
        finally:
            await db.dispose()

    asyncio.run(run())


@app.command("analyze")
def analyze(
    dump: Annotated[Path, typer.Option(help="JSONL produced by backfill")],
    window: Annotated[int, typer.Option(help="Minutes to group one author's messages")] = 30,
    sample: Annotated[int, typer.Option(help="How many examples to print")] = 10,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Run the parser offline against a backfill dump and report accuracy."""
    import sys

    _settings(verbose)

    from dsbot.jobs.analyze import analyze_records, read_records, write_report

    records = read_records(dump)
    result = analyze_records(records, window_minutes=window, sample_limit=sample)
    write_report(result, sys.stdout)


@app.command("doctor")
def doctor(
    guild: Annotated[int | None, typer.Option(help="Guild id to check")] = None,
    channel: Annotated[int | None, typer.Option(help="Channel id to check")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Verify token, intents, and permissions. Posts nothing."""
    settings = _settings(verbose)

    if not settings.discord_token:
        raise typer.BadParameter("DSBOT_DISCORD_TOKEN is empty in .env")

    async def run_doctor() -> None:
        import discord as _discord

        from dsbot.discord.preflight import describe, preflight

        intents = settings.intents()
        client = _discord.Client(intents=intents)
        runner: asyncio.Task[None] | None = None

        try:
            runner = asyncio.create_task(client.start(settings.discord_token))
            try:
                await _wait_until_ready(client, deadline_seconds=60.0, runner=runner)
            except _discord.PrivilegedIntentsRequired:
                typer.echo("--- connection ---")
                typer.echo("FAILED: a privileged intent is not enabled for this app.")
                typer.echo("")
                typer.echo("Fix in the Discord developer portal:")
                typer.echo("  1. https://discord.com/developers/applications")
                typer.echo("  2. select your application -> Bot")
                typer.echo("  3. Privileged Gateway Intents:")
                typer.echo("     [x] Message Content Intent   <- required to read report text")
                if settings.enable_members_intent:
                    typer.echo("     [x] Server Members Intent    <- enabled in .env")
                else:
                    typer.echo(
                        "     [ ] Server Members Intent    <- not needed yet "
                        "(set DSBOT_ENABLE_MEMBERS_INTENT=true when ranks land)"
                    )
                typer.echo("  4. Save Changes, then re-run `dsbot doctor`")
                raise typer.Exit(code=1) from None
            except _discord.LoginFailure:
                typer.echo("FAILED: the token was rejected. Reset it in the portal and")
                typer.echo("update DSBOT_DISCORD_TOKEN in .env (no quotes, no 'Bot ' prefix).")
                raise typer.Exit(code=1) from None

            target_guild = guild
            target_channel = channel

            # Fall back to the single bound channel when ids are omitted.
            if target_channel is None:
                db = Database(settings)
                try:
                    from dsbot.storage import repos

                    async with db.session() as session:
                        bindings = await repos.list_enabled_bindings(session)
                    if bindings:
                        target_channel = bindings[0].channel_id
                        target_guild = target_guild or bindings[0].guild_id
                finally:
                    await db.dispose()

            if target_guild is None or target_channel is None:
                typer.echo("no guild/channel given and none bound; run `dsbot bind` first")
                raise typer.Exit(code=1)

            info = describe(client, target_guild, target_channel)
            typer.echo("--- connection ---")
            for key, value in info.items():
                typer.echo(f"{key}: {value}")

            problems = await preflight(client, target_guild, target_channel)
            typer.echo("\n--- checks ---")
            if not problems:
                typer.echo("all checks passed")
            else:
                for problem in problems:
                    typer.echo(f"PROBLEM: {problem}")
                raise typer.Exit(code=1)
        finally:
            if not client.is_closed():
                await client.close()
            if runner is not None:
                runner.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await runner

    asyncio.run(run_doctor())


@app.command("say")
def say(
    channel: Annotated[int, typer.Option(help="Channel id")],
    message: Annotated[str, typer.Option("--message", "-m", help="Text to send")] = (
        "dsbot connectivity check"
    ),
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Post one message to a channel. Proves send permission works end to end."""
    settings = _settings(verbose)

    async def run_say() -> None:
        import discord as _discord

        client = _discord.Client(intents=_discord.Intents.none())
        runner: asyncio.Task[None] | None = None
        try:
            runner = asyncio.create_task(client.start(settings.discord_token))
            await _wait_until_ready(client, deadline_seconds=60.0, runner=runner)

            ch = client.get_channel(channel)
            if ch is None:
                ch = await client.fetch_channel(channel)
            if not isinstance(ch, _discord.abc.Messageable):
                raise typer.BadParameter(f"channel {channel} is not messageable")

            sent = await ch.send(message)
            typer.echo(f"sent message_id={sent.id} to #{getattr(ch, 'name', channel)}")
        except _discord.Forbidden as exc:
            typer.echo(f"FAILED: missing permission to send in {channel}: {exc}")
            raise typer.Exit(code=1) from None
        finally:
            if not client.is_closed():
                await client.close()
            if runner is not None:
                runner.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await runner

    asyncio.run(run_say())


@app.command("replay")
def replay(
    channel: Annotated[int, typer.Option(help="Channel id")],
    hours: Annotated[int, typer.Option(help="Look back this many hours")] = 24,
    post: Annotated[
        bool, typer.Option("--post", help="Actually send/clear warnings in Discord")
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Re-run stored messages through the reports module and show every decision."""
    settings = _settings(verbose)

    from dsbot.discord.reply import DiscordReplySink, RecordingReplySink
    from dsbot.jobs.replay import default_since, replay_channel
    from dsbot.modules.reports.service import ReportService

    async def run_replay() -> None:
        db = Database(settings)
        client = None
        runner = None
        try:
            if post:
                import discord as _discord

                client = _discord.Client(intents=settings.intents())
                runner = asyncio.create_task(client.start(settings.discord_token))
                await _wait_until_ready(client, deadline_seconds=60.0, runner=runner)
                sink = DiscordReplySink(client)
                typer.echo("mode: POSTING to Discord")
            else:
                sink = RecordingReplySink()
                typer.echo("mode: dry (no messages sent; use --post to send)")

            service = ReportService(
                db,
                sink,
                group_window=timedelta(seconds=settings.report_group_window_seconds),
            )
            result = await replay_channel(
                db,
                service,
                channel_id=channel,
                since=default_since(hours),
            )

            for line in result.lines:
                typer.echo(line)
            typer.echo("")
            typer.echo(json.dumps(result.summary(), indent=2))
            if isinstance(sink, RecordingReplySink):
                typer.echo(f"would_send={len(sink.sent)} would_clear={len(sink.cleared)}")
                for _, body in sink.sent:
                    typer.echo(f"  -> {body.splitlines()[0][:120]}")
        finally:
            if client is not None and not client.is_closed():
                await client.close()
            if runner is not None:
                runner.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await runner
            await db.dispose()

    asyncio.run(run_replay())


@app.command("scan")
def scan(
    channel: Annotated[int, typer.Option(help="Channel id to scan")],
    limit: Annotated[int, typer.Option(help="How many recent messages to read")] = 100,
    locale: Annotated[str | None, typer.Option(help="Language for issue text")] = None,
    out: Annotated[
        Path | None, typer.Option(help="Write the report to a file instead of the console")
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Read recent messages and print a verdict for each. Read-only.

    Deliberately ignores reactions: this judges the FORM only, so you can check the
    parser against real traffic yourself. Nothing is written and nothing is posted.
    """
    settings = _settings(verbose)

    from dsbot.jobs.scan import ScanMessage, render, scan_messages
    from dsbot.modules.reports.parser import parse_report  # noqa: F401
    from dsbot.modules.reports.schema import from_definition
    from dsbot.storage import repos

    async def run_scan() -> None:
        import discord as _discord

        db = Database(settings)
        client = None
        runner: asyncio.Task[None] | None = None
        try:
            async with db.session() as session:
                binding = await repos.get_binding_by_channel(session, channel)
                if binding is None:
                    typer.echo(f"channel {channel} is not bound; run `dsbot bind` first")
                    raise typer.Exit(code=1)
                rows = await repos.get_schemas_for_binding(session, binding.id)
                schemas = [from_definition(row.definition) for row in rows]
                binding_locale = binding.locale

            if not schemas:
                typer.echo(f"channel {channel} has no form bound")
                raise typer.Exit(code=1)

            client = _discord.Client(intents=_discord.Intents.none())
            runner = asyncio.create_task(client.start(settings.discord_token))
            await _wait_until_ready(client, deadline_seconds=60.0, runner=runner)

            target = client.get_channel(channel)
            if target is None:
                target = await client.fetch_channel(channel)
            if not isinstance(target, _discord.abc.Messageable):
                typer.echo(f"channel {channel} is not a text channel")
                raise typer.Exit(code=1)

            collected: list[ScanMessage] = []
            # history() is newest-first; read backwards then restore chronological order.
            async for message in target.history(limit=limit):  # type: ignore[attr-defined]
                if message.author.bot:
                    continue
                collected.append(
                    ScanMessage(
                        message_id=message.id,
                        author_id=message.author.id,
                        author_name=getattr(message.author, "display_name", None)
                        or str(message.author),
                        content=message.content or "",
                        created_at=message.created_at,
                        attachment_count=len(message.attachments),
                        attachment_names=tuple(a.filename for a in message.attachments),
                    )
                )
            collected.reverse()

            report = scan_messages(collected, schemas, locale=locale or binding_locale or "ru")
            if out is not None:
                out.parent.mkdir(parents=True, exist_ok=True)
                with out.open("w", encoding="utf-8") as handle:
                    render(report, handle, locale=locale or binding_locale or "ru")
                typer.echo(f"wrote {out}")
            else:
                import sys

                render(report, sys.stdout, locale=locale or binding_locale or "ru")
        finally:
            if client is not None and not client.is_closed():
                await client.close()
            if runner is not None:
                runner.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await runner
            await db.dispose()

    asyncio.run(run_scan())


@app.command("stop")
def stop(
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Stop the running bot, however it was started.

    Closing the terminal does not stop the bot: `uv run` leaves the child process alive,
    which is why messages keep arriving after Ctrl+C. Use this instead.
    """
    _settings(verbose)

    from dsbot.core.lock import DEFAULT_PID_PATH, is_running, read_pid, stop_running

    pid = read_pid(DEFAULT_PID_PATH)
    if pid is None:
        typer.echo("not running (no pid file)")
        return

    outcome = stop_running()
    typer.echo(outcome)
    if is_running(pid):
        typer.echo(f"If it refuses to die, terminate it directly:\n  Stop-Process -Id {pid} -Force")
        raise typer.Exit(code=1)


@app.command("status")
def status(
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Show whether the bot is running, and which process."""
    _settings(verbose)

    from dsbot.core.lock import DEFAULT_PID_PATH, is_running, read_pid

    pid = read_pid(DEFAULT_PID_PATH)
    if pid is None:
        typer.echo("stopped (no pid file)")
        return
    if is_running(pid):
        typer.echo(f"running (pid {pid})")
    else:
        typer.echo(f"stopped (stale pid file, pid {pid} is gone)")
        # A pid file left behind by a crash or a force kill would make the next
        # `dsbot stop` act on a recycled pid, so clear it here.
        from dsbot.core.lock import clear_pid

        clear_pid(DEFAULT_PID_PATH)


@app.command("run")
def run(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Parse and log but never post to Discord")
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Start the live bot. Ctrl+C to stop."""
    settings = _settings(verbose)

    from dsbot.bot import Bot, run_bot
    from dsbot.discord.reply import RecordingReplySink

    async def main() -> None:
        if dry_run:
            bot = Bot(settings, reply=RecordingReplySink())
        else:
            await run_bot(settings)
            return
        # Dry run: still connect and ingest, but never touch the channel.
        try:
            await bot.start(settings.discord_token)
        finally:
            await bot.close()

    asyncio.run(main())


@app.command("form-set")
def form_set(
    channel: Annotated[int, typer.Option(help="Channel id")],
    field: Annotated[str, typer.Option(help="Field id, e.g. qualification")],
    add: Annotated[
        list[str] | None,
        typer.Option("--add", help="Allowed value to add (repeatable, or comma-separated)"),
    ] = None,
    remove: Annotated[
        list[str] | None,
        typer.Option("--remove", help="Allowed value to remove (repeatable, or comma-separated)"),
    ] = None,
    set_values: Annotated[
        list[str] | None,
        typer.Option(
            "--set",
            help="Replace the list. Repeat the flag, or pass comma/space separated values",
        ),
    ] = None,
    required: Annotated[
        bool | None, typer.Option("--required/--optional", help="Set required-ness")
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Edit a field's allowed values (options or formations) for a channel.

    This is the day-to-day admin path. Examples:

        dsbot form-set --channel <id> --field qualification --add MED --add ENG
        dsbot form-set --channel <id> --field formation_idn --set SOB ARC RC
        dsbot form-set --channel <id> --field qualification --remove SNP
        dsbot form-set --channel <id> --field qualification --required
    """
    settings = _settings(verbose)

    from dsbot.modules.reports.authoring import edit_field_values
    from dsbot.modules.reports.schema import FormSchemaDef

    def split_values(values: list[str] | None) -> list[str]:
        """Accept repeated flags and comma/space separated lists alike."""
        import re as _re

        out: list[str] = []
        for chunk in values or []:
            out.extend(v for v in _re.split(r"[,\s]+", chunk) if v)
        return out

    async def run_set() -> None:
        db = Database(settings)
        try:
            async with db.write_session() as session:
                definition = await edit_field_values(
                    session,
                    channel_id=channel,
                    field_id=field,
                    add=split_values(add),
                    remove=split_values(remove),
                    replace=split_values(set_values) if set_values is not None else None,
                    required=required,
                )
        finally:
            await db.dispose()

        parsed = FormSchemaDef.model_validate(definition)
        spec = parsed.field_by_id(field)
        typer.echo(f"updated {parsed.key}.{field}")
        if spec is not None:
            if spec.type == "enum":
                typer.echo(f"  options   = {spec.options}")
            elif spec.type == "formation_idn":
                typer.echo(f"  formations= {spec.formations}")
            typer.echo(f"  required  = {spec.required}")

    try:
        asyncio.run(run_set())
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


@app.command("form-show")
def form_show(
    channel: Annotated[int, typer.Option(help="Channel id")],
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Show a channel's form in a compact, readable summary."""
    settings = _settings(verbose)

    from dsbot.modules.reports.authoring import export_forms
    from dsbot.modules.reports.schema import FormSchemaDef

    async def run_show() -> None:
        db = Database(settings)
        try:
            async with db.session() as session:
                bundle = await export_forms(session, channel)
        finally:
            await db.dispose()

        if not bundle.schemas:
            typer.echo(f"channel {channel} has no form bound")
            return
        for raw in bundle.schemas:
            schema = FormSchemaDef.model_validate(raw)
            typer.echo(f"\n[{schema.key}] {schema.title}")
            for spec in schema.fields:
                flag = "required" if spec.required else "optional"
                values = ""
                if spec.type == "enum":
                    values = f"  options={spec.options}"
                elif spec.type == "formation_idn":
                    values = f"  formations={spec.formations}"
                typer.echo(
                    f"  {spec.path or '-':<5} {spec.id:<18} {spec.type:<12} {flag:<8}{values}"
                )

    asyncio.run(run_show())


@app.command("form-check")
def form_check(
    file: Annotated[Path, typer.Option("--file", "-f", help="YAML form to check")],
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Validate a YAML form without applying it. Safe to run as often as you like."""
    _settings(verbose)

    from dsbot.modules.reports.yaml_form import compile_problems, parse_form_file

    try:
        schema = parse_form_file(file)
    except Exception as exc:
        typer.echo(f"INVALID: {exc}")
        raise typer.Exit(code=1) from None

    problems = compile_problems(schema)
    typer.echo(f"ok: {schema.key} ({len(schema.fields)} fields, {len(schema.rules)} rules)")
    for field in schema.fields:
        values = ""
        if field.type == "enum":
            values = f"  options={field.options}"
        elif field.type == "identity":
            values = f"  formations={field.formations}"
        typer.echo(f"  {field.path or '-':<4} {field.id:<18} {field.type:<20}{values}")
    for rule in schema.rules:
        when = " and ".join(f"{c.field} {c.op} {c.value!r}" for c in rule.when) or "always"
        effect = []
        exact = rule.exactly_attachments
        if exact is not None:
            effect.append(f"exactly {exact} attachments")
        else:
            if rule.min_attachments is not None:
                effect.append(f"min {rule.min_attachments} attachments")
            if rule.max_attachments is not None:
                effect.append(f"max {rule.max_attachments} attachments")
        if rule.allowed:
            effect.append(f"allowed {rule.allowed}")
        if rule.type:
            effect.append(f"must be {rule.type}")
        if not effect:
            effect.append("required")
        typer.echo(f"  rule {rule.id}: WHEN {when} -> {', '.join(effect)}")

    if problems:
        typer.echo("")
        for problem in problems:
            typer.echo(f"PROBLEM: {problem}")
        raise typer.Exit(code=1)


@app.command("form-apply")
def form_apply(
    channel: Annotated[int, typer.Option(help="Channel id")],
    file: Annotated[Path, typer.Option("--file", "-f", help="YAML form to apply")],
    force: Annotated[
        bool, typer.Option("--force", help="Apply even if checks report problems")
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Compile a YAML form and make it the channel's form. No code changes."""
    settings = _settings(verbose)

    from dsbot.modules.reports.authoring import import_forms
    from dsbot.modules.reports.yaml_form import compile_problems, parse_form_file

    async def run_apply() -> None:
        try:
            schema = parse_form_file(file)
        except Exception as exc:
            typer.echo(f"INVALID: {exc}")
            raise typer.Exit(code=1) from None

        problems = compile_problems(schema)
        if problems and not force:
            for problem in problems:
                typer.echo(f"PROBLEM: {problem}")
            typer.echo("not applied. Fix these, or re-run with --force")
            raise typer.Exit(code=1)

        db = Database(settings)
        try:
            async with db.write_session() as session:
                result = await import_forms(
                    session,
                    channel_id=channel,
                    payload={"schemas": [schema.model_dump(mode="json")]},
                )
        finally:
            await db.dispose()

        typer.echo(
            f"applied {schema.key} to channel {channel} "
            f"(created={result.created}, updated={result.updated})"
        )

    asyncio.run(run_apply())


@app.command("form-pull")
def form_pull(
    channel: Annotated[int, typer.Option(help="Channel id")],
    out: Annotated[
        Path | None, typer.Option(help="Where to write (default forms/ch-<id>.yaml)")
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Write the channel's current form out as an editable YAML file."""
    settings = _settings(verbose)
    target = out or Path("forms") / f"ch-{channel}.yaml"

    from dsbot.modules.reports.authoring import export_forms
    from dsbot.modules.reports.schema import FormSchemaDef
    from dsbot.modules.reports.yaml_form import form_to_yaml

    async def run_pull() -> None:
        db = Database(settings)
        try:
            async with db.session() as session:
                bundle = await export_forms(session, channel)
        finally:
            await db.dispose()

        if not bundle.schemas:
            typer.echo(f"channel {channel} has no form bound")
            raise typer.Exit(code=1)

        target.parent.mkdir(parents=True, exist_ok=True)
        blocks = [form_to_yaml(FormSchemaDef.model_validate(raw)) for raw in bundle.schemas]
        target.write_text("\n---\n".join(blocks), encoding="utf-8")
        typer.echo(f"wrote {target}")

    asyncio.run(run_pull())


@app.command("form-export")
def form_export(
    channel: Annotated[int, typer.Option(help="Channel id")],
    out: Annotated[
        Path | None, typer.Option(help="Output JSON path (default forms/ch-<id>.json)")
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Export a channel's form schema(s) as editable JSON."""
    settings = _settings(verbose)
    target = out or Path("forms") / f"ch-{channel}.json"

    from dsbot.modules.reports.authoring import export_forms

    async def run_export() -> None:
        db = Database(settings)
        try:
            async with db.session() as session:
                bundle = await export_forms(session, channel)
        finally:
            await db.dispose()

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(bundle.to_json(), encoding="utf-8")
        if not bundle.schemas:
            typer.echo(f"channel {channel} has no forms bound; wrote an empty bundle")
        typer.echo(f"wrote {target}")

    asyncio.run(run_export())


@app.command("form-template")
def form_template(
    out: Annotated[Path, typer.Option(help="Output JSON path")] = Path("forms/starter.json"),
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Write a blank starter form to edit for a new channel."""
    import json

    _settings(verbose)

    from dsbot.modules.reports.authoring import STARTER_DEFINITION

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"schemas": [STARTER_DEFINITION]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    typer.echo(f"wrote {out}")


@app.command("form-import")
def form_import(
    channel: Annotated[int, typer.Option(help="Channel id")],
    file: Annotated[Path, typer.Option("--file", "-f", help="Form JSON to import")],
    guild: Annotated[int | None, typer.Option(help="Guild id, for a new binding")] = None,
    rank_guild: Annotated[int | None, typer.Option(help="Rank source guild id")] = None,
    locale: Annotated[str, typer.Option(help="Bot message locale")] = "ru",
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Validate and import a form JSON, binding it to the channel."""
    settings = _settings(verbose)

    from dsbot.modules.reports.authoring import import_forms, load_definition_file

    async def run_import() -> None:
        payload = load_definition_file(file)
        db = Database(settings)
        try:
            async with db.write_session() as session:
                result = await import_forms(
                    session,
                    channel_id=channel,
                    payload=payload,
                    guild_id=guild,
                    rank_source_guild_id=rank_guild,
                    locale=locale,
                )
            typer.echo(
                f"imported {', '.join(result.imported)} "
                f"(created: {', '.join(result.created) or '-'}; "
                f"updated: {', '.join(result.updated) or '-'})"
            )
        finally:
            await db.dispose()

    try:
        asyncio.run(run_import())
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


@app.command("parse")
def parse_text(
    file: Annotated[Path, typer.Option("--file", "-f", help="Text file with a report")],
    channel: Annotated[
        int | None,
        typer.Option(help="Use this channel's real form instead of the built-in one"),
    ] = None,
    attachments: Annotated[int, typer.Option(help="Pretend N attachments present")] = 0,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Parse a report from a local file using a channel's form. Offline, no Discord."""
    import json

    settings = _settings(verbose)

    from dsbot.modules.reports.parser import GroupMessage, ReportCandidate, parse_report
    from dsbot.modules.reports.schema import DEFAULT_SCHEMAS, from_definition

    content = file.read_text(encoding="utf-8")

    async def load_schemas() -> list:
        if channel is None:
            return list(DEFAULT_SCHEMAS)
        from dsbot.storage import repos

        db = Database(settings)
        try:
            async with db.session() as session:
                binding = await repos.get_binding_by_channel(session, channel)
                if binding is None:
                    raise typer.BadParameter(f"channel {channel} is not bound")
                rows = await repos.get_schemas_for_binding(session, binding.id)
                return [from_definition(row.definition) for row in rows]
        finally:
            await db.dispose()

    schemas = asyncio.run(load_schemas())
    if not schemas:
        raise typer.BadParameter("channel has no form schemas bound")

    from datetime import UTC, datetime

    group = ReportCandidate(
        messages=[
            GroupMessage(
                message_id=0,
                author_id=0,
                created_at=datetime.now(UTC),
                content=content,
                attachment_count=attachments,
            )
        ]
    )
    result = parse_report(schemas, group)
    typer.echo(
        json.dumps(
            {
                "matched": result.matched,
                "schema": result.schema_key,
                "score": result.score,
                "valid": result.is_valid,
                "issues": [i.to_payload() for i in result.issues],
                "fields": [
                    {
                        "id": b.spec.id,
                        "resolution": b.resolution,
                        "present": b.result.present,
                        "raw": b.raw_value,
                        "value": b.result.value,
                    }
                    for b in result.fields
                ],
                "diagnostics": result.diagnostics,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
