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
from typing import Annotated, Any

import discord
import structlog
import typer

from dsbot.config import Settings, get_settings
from dsbot.logging_setup import configure_logging
from dsbot.storage import admin, repos
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
            applied = await db.prepare()
            ok = await db.healthcheck()
            typer.echo(f"database ready: {settings.resolved_database_url}")
            typer.echo(f"state dir: {settings.state_dir()}")
            if applied:
                typer.echo(f"upgraded columns: {', '.join(applied)}")
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
        from dsbot.modules.reports.yaml_form import load_default_schemas
        from dsbot.storage import admin

        db = Database(settings)
        try:
            async with db.write_session() as session:
                await admin.ensure_schema_seed(session, load_default_schemas())
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
    parse: Annotated[
        bool,
        typer.Option(
            "--parse/--no-parse",
            help="Also parse stored messages into reports (needed for counting)",
        ),
    ] = True,
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
            report_service = None
            if to_db and parse and db is not None:
                # Backfill writes straight to the archive, so without this the reports
                # table stays empty and a count over the same period finds nothing.
                from dsbot.modules.reports.service import ReportService

                await db.prepare()
                report_service = ReportService(db)
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
                    parse=parse,
                    report_service=report_service,
                )
            finally:
                if writer is not None:
                    writer.__exit__()

            typer.echo(
                f"messages={stats.messages} reactions={stats.reactions} "
                f"skipped_bots={stats.skipped_bots} errors={stats.errors}"
            )
            if report_service is not None:
                typer.echo(f"parsed into reports: {stats.parsed}")
                if stats.parsed == 0 and stats.messages:
                    typer.echo(
                        "NOTE: no report matched the bound form. Check that the channel is "
                        "bound and the form file is the one the channel uses."
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
    all_instances: Annotated[
        bool,
        typer.Option("--all", help="Terminate every dsbot process, ignoring the instance name"),
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Stop this instance of the bot.

    Closing the terminal does not stop it: `uv run` leaves the child process alive,
    which is why messages keep arriving after Ctrl+C. Use this instead.

    Only the instance named by DSBOT_INSTANCE_NAME is stopped, so a test bot running
    next to the live one is left alone. `--all` is the escape hatch when the pid file
    was lost.
    """
    settings = _settings(verbose)

    from dsbot.core.lock import stop_all, stop_everything

    outcome = stop_everything() if all_instances else stop_all(lock_path=settings.lock_file())
    typer.echo(outcome)
    if "STILL RUNNING" in outcome:
        typer.echo("Terminate manually with: Stop-Process -Id <pid> -Force")
        raise typer.Exit(code=1)


@app.command("status")
def status(
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Show whether this instance is running, and which process."""
    settings = _settings(verbose)

    from dsbot.core.lock import clear_pid, is_running, read_pid

    pid_path = settings.pid_file()
    pid = read_pid(pid_path)
    typer.echo(f"instance: {settings.instance_name}")
    typer.echo(f"database: {settings.resolved_database_url}")
    typer.echo(f"state dir: {settings.state_dir()}")
    if pid is None:
        typer.echo("stopped (no pid file)")
        return
    if is_running(pid):
        typer.echo(f"running (pid {pid})")
    else:
        typer.echo(f"stopped (stale pid file, pid {pid} is gone)")
        # A pid file left behind by a crash or a force kill would make the next
        # `dsbot stop` act on a recycled pid, so clear it here.
        clear_pid(pid_path)


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


@app.command("ladder-check")
def ladder_check(
    file: Annotated[Path, typer.Option("--file", "-f", help="Ladder YAML to check")],
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Validate a rank ladder without applying it."""
    _settings(verbose)

    from dsbot.modules.ranks.ladder import ladder_problems, load_ladder

    try:
        spec = load_ladder(file)
    except Exception as exc:
        typer.echo(f"INVALID: {exc}")
        raise typer.Exit(code=1) from None

    typer.echo(
        f"ok: {spec.key} (guild {spec.guild_id}, {len(spec.tiers)} tiers, purpose {spec.purpose})"
    )
    for tier in sorted(spec.tiers, key=lambda t: t.ordinal, reverse=True):
        roles = ", ".join(str(r) for r in tier.roles) or "(none)"
        if spec.purpose == "quota":
            norm = (
                f"needs[{tier.required_per_week}/week]"
                if tier.required_per_week is not None
                else "needs[NOT SET]"
            )
            typer.echo(
                f"  {tier.ordinal:>4}  {tier.key:<20} {tier.label:<22} roles[{roles}]  {norm}"
            )
        else:
            caps = ", ".join(tier.capabilities) or "-"
            typer.echo(
                f"  {tier.ordinal:>4}  {tier.key:<20} {tier.label:<22} roles[{roles}]  caps[{caps}]"
            )

    problems = ladder_problems(spec)
    if problems:
        typer.echo("")
        for problem in problems:
            typer.echo(f"PROBLEM: {problem}")
        raise typer.Exit(code=1)


@app.command("ladder-apply")
def ladder_apply(
    file: Annotated[Path, typer.Option("--file", "-f", help="Ladder YAML to apply")],
    force: Annotated[
        bool, typer.Option("--force", help="Apply even if checks report problems")
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Load a rank ladder into the database right now, without restarting.

    Normally you do NOT need this: `forms/channels.yaml` names the ladders each channel
    uses, and startup applies them automatically, so pushing the file is enough. Use this
    when you want to apply a ladder immediately on a running bot, or to inspect what a
    file would produce. Note that a running bot only reloads ladders at startup.
    """
    settings = _settings(verbose)

    from dsbot.modules.ranks.ladder import ladder_problems, load_ladder
    from dsbot.storage import ranks_repo

    async def run_apply() -> None:
        try:
            spec = load_ladder(file)
        except Exception as exc:
            typer.echo(f"INVALID: {exc}")
            raise typer.Exit(code=1) from None

        problems = ladder_problems(spec)
        if problems and not force:
            for problem in problems:
                typer.echo(f"PROBLEM: {problem}")
            typer.echo("not applied. Fix these, or re-run with --force")
            raise typer.Exit(code=1)

        db = Database(settings)
        try:
            async with db.write_session() as session:
                ladder = await ranks_repo.replace_ladder(session, spec)
        finally:
            await db.dispose()

        typer.echo(
            f"applied ladder {ladder.key} (id {ladder.id}) for guild "
            f"{spec.guild_id}: {len(spec.tiers)} tiers"
        )

    asyncio.run(run_apply())


@app.command("ladder-show")
def ladder_show(
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Show every ladder in the database."""
    settings = _settings(verbose)

    from dsbot.storage import ranks_repo

    async def run_show() -> None:
        db = Database(settings)
        try:
            async with db.session() as session:
                ladders = await ranks_repo.list_ladders(session)
        finally:
            await db.dispose()

        if not ladders:
            typer.echo("no ladders configured. Use `dsbot ladder-apply`.")
            return
        for ladder, tiers in ladders:
            typer.echo(
                f"\n[{ladder.key}] {ladder.name or '-'} "
                f"(guild {ladder.guild_id}, purpose {ladder.purpose})"
            )
            for tier in tiers:
                roles = ", ".join(str(r) for r in tier["roles"]) or "(none)"
                caps = ", ".join(tier["capabilities"]) or "-"
                extra = ""
                if tier.get("required_per_week") is not None:
                    extra = f"  needs[{tier['required_per_week']}/week]"
                typer.echo(
                    f"  {tier['ordinal']:>4}  {tier['key']:<20} {tier['label']:<22} "
                    f"roles[{roles}] caps[{caps}]{extra}"
                )

    asyncio.run(run_show())


@app.command("disable")
def disable(
    channel: Annotated[int, typer.Option(help="Channel id")],
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Pause a channel: stop answering there, keep its form and history.

    Takes effect immediately on a running bot, because the binding is read per message.
    Re-enable with `dsbot enable`.
    """
    _set_channel_enabled(channel, False, verbose)


@app.command("enable")
def enable(
    channel: Annotated[int, typer.Option(help="Channel id")],
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Resume a paused channel, with its form and history intact."""
    _set_channel_enabled(channel, True, verbose)


def _set_channel_enabled(channel: int, enabled: bool, verbose: bool) -> None:
    settings = _settings(verbose)

    from dsbot.storage import admin

    async def run_set() -> None:
        db = Database(settings)
        try:
            async with db.write_session() as session:
                found = await admin.set_binding_enabled(session, channel, enabled)
        finally:
            await db.dispose()

        action = "enabled" if enabled else "disabled"
        if not found:
            typer.echo(f"channel {channel} is not bound to any form")
            raise typer.Exit(code=1)
        typer.echo(f"channel {channel} {action}")

    asyncio.run(run_set())


@app.command("channels-check")
def channels_check(
    file: Annotated[
        Path | None, typer.Option("--file", "-f", help="Channels YAML to check")
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Validate forms/channels.yaml without touching the database."""
    _settings(verbose)

    from dsbot.modules.reports.channels import (
        channels_problems,
        load_channels_file,
        seed_channels_file,
    )

    settings = get_settings()
    path = file or settings.resolved_channels_file()
    if file is None:
        try:
            if seed_channels_file(path, settings.seed_file()):
                typer.echo(f"seeded {path} from {settings.seed_file()}")
        except Exception as exc:
            typer.echo(f"could not seed {path}: {exc}", err=True)
    try:
        spec = load_channels_file(path)
    except Exception as exc:
        typer.echo(f"INVALID: {exc}")
        raise typer.Exit(code=1) from None

    typer.echo(f"ok: {len(spec.channels)} channel(s) in {path}")
    for entry in spec.channels:
        state = "enabled" if entry.enabled else "PAUSED"
        rank = (
            f"rank_guild={entry.rank_guild}"
            if entry.rank_guild is not None
            else "rank_guild=(same guild)"
        )
        ladders = f"review={entry.review_ladder or '-'} quota={entry.quota_ladder or '-'}"
        typer.echo(
            f"  guild {entry.guild}  channel {entry.channel}  "
            f"form={entry.form}  {rank}  {ladders}  locale={entry.locale}  {state}"
        )

    # Ladders are applied by this file, so a missing or invalid one is a configuration
    # error that must be caught before deploy, not at startup on the server.
    for problem in _ladder_file_problems(spec):
        typer.echo(f"PROBLEM: {problem}")

    problems = channels_problems(spec)
    if problems:
        typer.echo("")
        for problem in problems:
            typer.echo(f"PROBLEM: {problem}")
        raise typer.Exit(code=1)


@app.command("channels-apply")
def channels_apply(
    file: Annotated[
        Path | None, typer.Option("--file", "-f", help="Channels YAML to apply")
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Make the database match forms/channels.yaml, including its ladders.

    Startup does this automatically, so this command exists for checking the result
    by hand; nothing is deleted, and running it twice changes nothing the second time.
    """
    settings = _settings(verbose)

    from dsbot.modules.reports.channels import load_channels_file, seed_channels_file
    from dsbot.modules.reports.channels_sync import sync_channels

    path = file or settings.resolved_channels_file()
    if file is None:
        try:
            seed_channels_file(path, settings.seed_file())
        except Exception as exc:
            typer.echo(f"could not seed {path}: {exc}", err=True)

    async def run_apply() -> None:
        try:
            spec = load_channels_file(path)
        except Exception as exc:
            typer.echo(f"INVALID: {exc}")
            raise typer.Exit(code=1) from None

        db = Database(settings)
        try:
            async with db.write_session() as session:
                result = await sync_channels(session, spec)
        except FileNotFoundError as exc:
            typer.echo(f"FAILED: {exc}")
            raise typer.Exit(code=1) from None
        finally:
            await db.dispose()

        typer.echo(
            f"created={result.created} updated={result.updated} "
            f"paused={result.paused} unchanged={len(result.unchanged)}"
        )
        if result.ladders:
            typer.echo(f"ladders applied: {', '.join(result.ladders)}")

    asyncio.run(run_apply())


@app.command("channels")
def channels(
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """List every bound channel, its server, its form, and whether it is active."""
    settings = _settings(verbose)

    from dsbot.storage import admin, repos

    async def run_list() -> None:
        db = Database(settings)
        try:
            async with db.session() as session:
                bindings = await admin.list_all_bindings(session)
                rows = []
                for binding in bindings:
                    schemas = await repos.get_schemas_for_binding(session, binding.id)
                    guild = await session.get(
                        __import__("dsbot.storage.models", fromlist=["Guild"]).Guild,
                        binding.guild_id,
                    )
                    rows.append(
                        (
                            binding.channel_id,
                            binding.guild_id,
                            getattr(guild, "name", "") or "?",
                            binding.module_key,
                            ", ".join(s.key for s in schemas) or "(none)",
                            binding.locale,
                            "active" if binding.enabled else "PAUSED",
                        )
                    )
        finally:
            await db.dispose()

        if not rows:
            typer.echo("no channels bound. Add one to forms/channels.yaml and redeploy.")
            return

        typer.echo(
            f"{'channel':>20}  {'server':>20}  {'module':<8} {'form':<20} {'locale':<6} state"
        )
        for channel_id, guild_id, guild_name, module, forms, locale, state in rows:
            typer.echo(
                f"{channel_id:>20}  {guild_id:>20}  {module:<8} {forms:<20} "
                f"{locale:<6} {state}   [{guild_name}]"
            )

    asyncio.run(run_list())


@app.command("form-apply")
def form_apply(
    channel: Annotated[int, typer.Option(help="Channel id")],
    file: Annotated[Path, typer.Option("--file", "-f", help="YAML form to apply")],
    guild: Annotated[
        int | None,
        typer.Option(help="Guild id, required when binding a channel in a new server"),
    ] = None,
    rank_guild: Annotated[
        int | None, typer.Option(help="Guild whose rank ladder governs this channel")
    ] = None,
    locale: Annotated[str, typer.Option(help="Bot message locale: ru or en")] = "ru",
    force: Annotated[
        bool, typer.Option("--force", help="Apply even if checks report problems")
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Compile a YAML form and make it the channel's form. No code changes.

    To set up a channel in another server, pass both ids:

        dsbot form-apply --channel <id> --file forms/training.yaml --guild <id>
    """
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

        # Resolve real names while we can talk to Discord, so the channels listing is
        # readable and the binding records where it belongs.
        guild_name = ""
        channel_name = ""
        import discord as _discord

        client = _discord.Client(intents=_discord.Intents.none())
        runner: asyncio.Task[None] | None = None
        try:
            runner = asyncio.create_task(client.start(settings.discord_token))
            await _wait_until_ready(client, deadline_seconds=60.0, runner=runner)

            if guild is not None:
                target = client.get_guild(guild)
                # get_guild can return a cached stub whose name is empty. Treat that as
                # "not resolved" and fetch, otherwise the stored name stays blank.
                if target is None or not target.name:
                    try:
                        target = await client.fetch_guild(guild)
                    except Exception as exc:
                        typer.echo(
                            f"WARNING: cannot see guild {guild}: {type(exc).__name__}: {exc}"
                        )
                        target = None
                if target is not None and target.name:
                    guild_name = target.name

            try:
                found = client.get_channel(channel)
                if found is None:
                    found = await client.fetch_channel(channel)
                channel_name = getattr(found, "name", "") or ""
            except Exception:
                typer.echo(f"WARNING: cannot see channel {channel}")
        except Exception:
            typer.echo("WARNING: could not reach Discord to confirm names")
        finally:
            if not client.is_closed():
                await client.close()
            if runner is not None:
                runner.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await runner

        db = Database(settings)
        try:
            async with db.write_session() as session:
                from dsbot.storage import admin, repos

                if guild is not None and guild_name:
                    await admin.refresh_guild_names(session, {guild: guild_name})
                resolved_guild = guild
                if resolved_guild is None:
                    existing = await repos.get_binding_by_channel(session, channel)
                    resolved_guild = existing.guild_id if existing else None
                if resolved_guild is not None:
                    await admin.ensure_guild_and_channel(
                        session,
                        guild_id=resolved_guild,
                        channel_id=channel,
                        guild_name=guild_name,
                        channel_name=channel_name,
                    )
                result = await import_forms(
                    session,
                    channel_id=channel,
                    payload={"schemas": [schema.model_dump(mode="json")]},
                    guild_id=guild,
                    rank_source_guild_id=rank_guild,
                    locale=locale,
                )
        except ValueError as exc:
            typer.echo(f"FAILED: {exc}")
            raise typer.Exit(code=1) from None
        finally:
            await db.dispose()

        place = f"#{channel_name}" if channel_name else f"channel {channel}"
        where = f" in {guild_name}" if guild_name else (f" in guild {guild}" if guild else "")
        typer.echo(
            f"applied {schema.key} to {place}{where} "
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
    from dsbot.modules.reports.schema import from_definition

    content = file.read_text(encoding="utf-8")

    async def load_schemas() -> list:
        if channel is None:
            from dsbot.modules.reports.yaml_form import load_default_schemas

            return load_default_schemas()
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


# --- phase 3: counting, roster and quotas ---------------------------------


def _ladder_file_problems(spec: Any) -> list[str]:
    """Check that every ladder a channel names exists, parses, and matches its guild.

    Ladders are applied by `forms/channels.yaml`, so this is the validation step that
    keeps a typo from reaching the server, where it would only show up as "nobody can
    approve" at runtime.
    """
    from dsbot.modules.ranks.ladder import ladder_problems, load_ladder
    from dsbot.modules.reports.channels_sync import LADDER_DIR

    problems: list[str] = []
    for entry in spec.channels:
        guild = entry.rank_source_guild
        for key in (entry.review_ladder, entry.quota_ladder):
            if not key:
                continue
            path = LADDER_DIR / f"{key}.yaml"
            if not path.is_file():
                problems.append(
                    f"channel {entry.channel}: ladder {key!r} not found at {path}. "
                    "Create it (see review.yaml.example / quota.yaml.example)."
                )
                continue
            try:
                parsed = load_ladder(path)
            except Exception as exc:
                problems.append(f"{path}: INVALID: {exc}")
                continue
            if parsed.key != key:
                problems.append(
                    f"{path}: declares key {parsed.key!r} but channel {entry.channel} asked "
                    f"for {key!r}"
                )
            if parsed.guild_id != guild:
                problems.append(
                    f"{path}: is for guild {parsed.guild_id}, but channel {entry.channel} "
                    f"reads roles from guild {guild}"
                )
            expected = "quota" if key == entry.quota_ladder else "review"
            if parsed.purpose != expected:
                problems.append(
                    f"{path}: purpose is {parsed.purpose!r}, but it is named as "
                    f"{expected!r} for channel {entry.channel}"
                )
            for issue in ladder_problems(parsed):
                problems.append(f"{path}: {issue}")
    return problems


def _period_window(
    week: str,
    weeks: int | None,
    date_from: str | None,
    date_to: str | None,
    timezone: str | None = None,
    days: int | None = None,
) -> Any:
    """Resolve the period the operator asked for.

    Delegates to `resolve_period` so the CLI and `/reports count` cannot disagree about
    what a period means. `--from`/`--to` win over a relative window, because someone who
    typed two dates meant those dates.
    """
    from dsbot.modules.quotas.period import resolve_period

    zone = timezone or get_settings().default_timezone
    try:
        return resolve_period(
            timezone=zone,
            start=date_from,
            end=date_to,
            days=days,
            weeks=weeks,
            which=week,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None


def _require_scrape_settings(settings: Settings) -> None:
    """Fail early and specifically when the roster cannot possibly be read.

    Both failures are configuration, not bugs, and each has a one-line fix. Reporting
    them before connecting beats a stack trace from deep inside discord.py.
    """
    if not settings.discord_token:
        raise typer.BadParameter(
            "DSBOT_DISCORD_TOKEN is empty, so Discord cannot be asked who holds a role"
        )
    if not settings.enable_members_intent:
        raise typer.BadParameter(
            "the Members intent is off (DSBOT_ENABLE_MEMBERS_INTENT=false). Discord "
            "refuses to list guild members without it, so people with no reports would "
            "be missing from the count instead of showing 0. Enable SERVER MEMBERS "
            "INTENT in the developer portal and set it to true."
        )


def _new_lookup_client() -> Any:
    """A Discord client with exactly the intents a roster scrape needs."""
    import discord

    return discord.Client(intents=_lookup_intents())


def _lookup_intents() -> Any:
    """Guilds plus Members, nothing else.

    Deliberately minimal: this connection only lists members and sends a report. It must
    not request Message Content, so it cannot be mistaken for a second ingest path.
    """
    import discord

    intents = discord.Intents.none()
    intents.guilds = True
    intents.members = True
    return intents


async def _with_lookup_client(settings: Settings, work: Any) -> Any:
    """Connect once, run `work(client)`, disconnect.

    discord.py only exposes a ready hook through a client subclass, so this wraps the
    pattern in one place instead of repeating the connect/ready/close dance per command.
    """
    import discord

    box: dict[str, Any] = {}

    class _Once(discord.Client):
        async def on_ready(self) -> None:
            try:
                box["value"] = await work(self)
            except Exception as exc:
                box["error"] = exc
            finally:
                await self.close()

    if not settings.discord_token:
        raise typer.BadParameter("DSBOT_DISCORD_TOKEN is empty")
    client = _Once(intents=_lookup_intents())
    await client.start(settings.discord_token)
    if "error" in box:
        raise box["error"]
    return box.get("value")


@app.command("roster-refresh")
def roster_refresh(
    channel: Annotated[int, typer.Option(help="Report channel whose roster to scrape")],
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Scrape everyone holding a ladder role into the database.

    Kept separate from `count` on purpose: scraping needs a gateway connection, counting
    reads the stored snapshot. That way a count is reproducible offline, and the live
    bot can serve a count without a restart.
    """
    settings = _settings(verbose)
    _require_scrape_settings(settings)

    async def run() -> None:
        from dsbot.modules.ranks.roster import DiscordRosterSource, RosterService
        from dsbot.storage import ranks_repo

        db = Database(settings)
        try:
            async with db.session() as session:
                binding = await repos.get_binding_by_channel(session, channel)
                if binding is None:
                    typer.echo(f"channel {channel} is not bound to any form")
                    raise typer.Exit(code=1)
                ladders = await ranks_repo.load_all_ladders(session)

            guild_id = binding.rank_source_guild_id or binding.guild_id
            ladder = ladders.get(guild_id)
            if ladder is None or ladder.quota_ladder_id is None:
                typer.echo(
                    f"no ladder for guild {guild_id}. Apply one first: "
                    "uv run dsbot ladder-apply --file forms/ladders/instructors.yaml"
                )
                raise typer.Exit(code=1)

            async def work(client: Any) -> Any:
                service = RosterService(db, DiscordRosterSource(client))
                # No `requires`: this is the population that owes reports. Review
                # authority is asked for separately, when a reaction arrives.
                return await service.refresh(
                    guild_id,
                    ladder,
                    ladder_id=ladder.quota_ladder_id or 0,
                )

            try:
                result = await _with_lookup_client(settings, work)
            except Exception:
                typer.echo("could not connect to Discord to read members", err=True)
                raise typer.Exit(code=1) from None

            if result is None or not result.ok:
                detail = getattr(result, "error", None) or "no members returned"
                typer.echo(f"roster not refreshed: {detail}", err=True)
                raise typer.Exit(code=1)
            typer.echo(
                f"roster refreshed: {len(result.members)} of {result.total_members} "
                f"member(s) in guild {guild_id}"
            )
        finally:
            await db.dispose()

    asyncio.run(run())


@app.command("roster")
def roster_show(
    channel: Annotated[int, typer.Option(help="Report channel whose roster to show")],
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Show the stored roster that counting uses."""
    settings = _settings(verbose)

    async def run() -> None:
        from dsbot.storage import ranks_repo

        db = Database(settings)
        try:
            async with db.session() as session:
                binding = await repos.get_binding_by_channel(session, channel)
                if binding is None:
                    typer.echo(f"channel {channel} is not bound to any form")
                    raise typer.Exit(code=1)
                ladders = await ranks_repo.load_all_ladders(session)
                guild_id = binding.rank_source_guild_id or binding.guild_id
                ladder = ladders.get(guild_id)
                if ladder is None or ladder.quota_ladder_id is None:
                    typer.echo(f"no ladder for guild {guild_id}")
                    raise typer.Exit(code=1)
                rows = await repos.list_roster(session, ladder.quota_ladder_id)
        finally:
            await db.dispose()

        if not rows:
            typer.echo(
                "roster is empty. Scrape it once with:\n"
                f"  uv run dsbot roster-refresh --channel {channel}"
            )
            return
        typer.echo(f"{len(rows)} person(s) counted for (guild {guild_id})")
        for row in rows:
            ordinal = row.tier_ordinal if row.tier_ordinal is not None else "-"
            needs = f"{row.required_per_week}/week" if row.required_per_week else "no quota"
            typer.echo(
                f"  {ordinal!s:>4}  {row.tier_label or '-':<22} {needs:<10} "
                f"{row.user_id:<20} {row.display_name}"
            )

    asyncio.run(run())


@app.command("count")
def count_command(
    channel: Annotated[int, typer.Option(help="Report channel id to count for")],
    week: Annotated[str, typer.Option(help="'current' (default) or 'last'")] = "current",
    weeks: Annotated[
        int | None, typer.Option(help="Last N ISO weeks including the current one")
    ] = None,
    days: Annotated[int | None, typer.Option(help="Last N days, counting back from now")] = None,
    date_from: Annotated[
        str | None, typer.Option("--from", help="Start date, YYYY-MM-DD or DD.MM.YYYY")
    ] = None,
    date_to: Annotated[str | None, typer.Option("--to", help="End date, inclusive")] = None,
    csv_out: Annotated[
        Path | None, typer.Option("--csv", help="Write the result to this CSV file")
    ] = None,
    hide_tiers: Annotated[
        str | None,
        typer.Option("--hide-tiers", help="Comma-separated tiers to leave out of the message"),
    ] = None,
    save: Annotated[bool, typer.Option(help="Store this run so a re-run updates it")] = False,
    publish: Annotated[int | None, typer.Option(help="Channel id to post the report to")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Count approved reports per person for any period."""
    settings = _settings(verbose)

    async def run() -> None:
        from dsbot.discord.reply import DiscordReplySink
        from dsbot.modules.quotas.service import CountingService, CountRequest

        db = Database(settings)
        try:
            async with db.session() as session:
                binding = await repos.get_binding_by_channel(session, channel)
            if binding is None:
                typer.echo(f"channel {channel} is not bound to any form")
                raise typer.Exit(code=1)

            # A CLI count may run against a database last upgraded by an older build.
            await db.prepare()

            # The channel may override the deployment timezone, so the period is
            # resolved after the binding is known. Otherwise a shell count and a slash
            # count of the same week could disagree by hours.
            config = binding.config or {}
            timezone = config.get("timezone") or settings.default_timezone
            period = _period_window(week, weeks, date_from, date_to, timezone, days)

            persist = save or publish is not None
            request = CountRequest(
                binding_id=binding.id,
                channel_id=channel,
                period=period,
                timezone=timezone,
            )

            # --hide-tiers on the command line beats whatever the channel stores, so an
            # operator can look at one report without changing configuration.
            hidden = (
                tuple(t.strip() for t in hide_tiers.split(",") if t.strip())
                if hide_tiers
                else tuple(str(v) for v in (config.get("hidden_tiers") or []))
            )

            if publish is None:
                # Counting is a pure database read; no Discord connection needed.
                outcome = await CountingService(
                    db,
                    developer_user_id=settings.developer_user_id,
                ).count(
                    request,
                    scope=f"Канал: <#{channel}>",
                    persist=persist,
                    hidden_tiers=hidden,
                )
                _print_outcome(outcome, csv_out)
                return

            target_channel = publish

            async def work(client: Any) -> Any:
                service = CountingService(
                    db,
                    DiscordReplySink(client),
                    developer_user_id=settings.developer_user_id,
                )
                counted = await service.count(
                    request,
                    scope=f"Канал: <#{channel}>",
                    persist=persist,
                    hidden_tiers=hidden,
                )
                _print_outcome(counted, csv_out)
                posted = await service.publish(request, counted, target_channel_id=target_channel)
                typer.echo(f"posted to {target_channel}: {len(posted)} message(s)")
                return counted

            await _with_lookup_client(settings, work)
        finally:
            await db.dispose()

    asyncio.run(run())


def _print_outcome(outcome: Any, csv_out: Path | None) -> None:
    for message in outcome.messages():
        typer.echo(message)
        typer.echo("")
    for problem in outcome.problems:
        typer.echo(f"PROBLEM: {problem}", err=True)
    if csv_out is not None:
        csv_out.parent.mkdir(parents=True, exist_ok=True)
        csv_out.write_text(outcome.csv(), encoding="utf-8")
        typer.echo(f"written: {csv_out}")
    if outcome.roster_missing:
        typer.echo(
            "Подсказка: роли ещё не заданы, поэтому список людей для подсчёта неизвестен.\n"
            "  uv run dsbot ladder-apply --file forms/ladders/instructors.yaml\n"
            "  uv run dsbot roster-refresh --channel <id>",
            err=True,
        )


@app.command("report-format")
def report_format(
    channel: Annotated[int, typer.Option(help="Report channel id")],
    publish_to: Annotated[
        int | None,
        typer.Option(
            "--publish-to",
            help="Channel that receives the periodic report; 0 clears it",
        ),
    ] = None,
    timezone: Annotated[
        str | None,
        typer.Option(help="Timezone for week boundaries on this channel, e.g. Europe/Moscow"),
    ] = None,
    show: Annotated[bool, typer.Option("--show", help="Only show the current setting")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Set where and in which timezone the report is produced.

    The destination lives in the binding, not in code: an admin decides it per channel,
    which is what "publish it to another channel set by them" means in practice. The
    timezone matters because it decides where a week ends, and a channel may legitimately
    sit in a different timezone from the deployment default.
    """
    settings = _settings(verbose)

    async def run() -> None:
        from dsbot.modules.quotas.period import load_timezone

        db = Database(settings)
        try:
            async with db.session() as session:
                binding = await repos.get_binding_by_channel(session, channel)
            if binding is None:
                typer.echo(f"channel {channel} is not bound to any form")
                raise typer.Exit(code=1)

            config = dict(binding.config or {})
            if show or (publish_to is None and timezone is None):
                typer.echo(f"channel {channel}")
                typer.echo(f"  publish_to: {config.get('report_channel_id') or '(ephemeral only)'}")
                typer.echo(f"  timezone:   {config.get('timezone') or settings.default_timezone}")
                return

            if publish_to is not None:
                config["report_channel_id"] = None if publish_to == 0 else publish_to
            if timezone is not None:
                # Validate before storing: an unknown name would otherwise only surface
                # at the next count, hours later, as a failed command.
                try:
                    load_timezone(timezone)
                except ValueError as exc:
                    typer.echo(f"INVALID: {exc}")
                    raise typer.Exit(code=1) from None
                config["timezone"] = timezone

            async with db.write_session() as session:
                row = await repos.get_binding_by_channel(session, channel)
                assert row is not None
                row.config = config
            typer.echo("updated. In effect on the next count.")
        finally:
            await db.dispose()

    asyncio.run(run())


@app.command("channel-ladder")
def channel_ladder(
    channel: Annotated[int, typer.Option(help="Report channel id")],
    ladder: Annotated[
        str | None,
        typer.Option(help="Ladder key to pin this channel to; empty means 'any in the guild'"),
    ] = None,
    show: Annotated[bool, typer.Option("--show", help="Only show the current setting")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Pin a channel's hierarchy to one ladder in its rank-source guild.

    This is the channel-level hierarchy: two channels can read reports in the same guild
    while ordering the same people differently, because each binding points at its own
    ladder. The *roles* still come from the other Discord server (`rank-source guild`),
    which is what makes cross-server approval work.

    Leave it unset to let every ladder in the guild compete, with the highest ordinal
    winning. Pin it when a channel must not inherit a neighbouring ladder's ordering.
    """
    settings = _settings(verbose)

    async def run() -> None:
        from sqlalchemy import select

        from dsbot.storage.models import RankLadder

        db = Database(settings)
        try:
            async with db.session() as session:
                binding = await repos.get_binding_by_channel(session, channel)
                if binding is None:
                    typer.echo(f"channel {channel} is not bound to any form")
                    raise typer.Exit(code=1)
                guild_id = binding.rank_source_guild_id or binding.guild_id
                candidates = (
                    await session.scalars(select(RankLadder).where(RankLadder.guild_id == guild_id))
                ).all()
                current = binding.rank_ladder_id
                target = None
                if ladder is not None and ladder.strip():
                    target = next((row for row in candidates if row.key == ladder.strip()), None)
                    if target is None:
                        known = ", ".join(sorted(row.key for row in candidates)) or "(none)"
                        typer.echo(f"no ladder {ladder!r} in guild {guild_id}. Known: {known}")
                        raise typer.Exit(code=1)

                if show or ladder is None:
                    pinned = next((r.key for r in candidates if r.id == current), None)
                    typer.echo(f"channel {channel} (rank guild {guild_id})")
                    typer.echo(f"  pinned ladder: {pinned or '(any ladder in the guild)'}")
                    typer.echo(
                        "  known ladders: " + (", ".join(sorted(r.key for r in candidates)) or "-")
                    )
                    return

                async with db.write_session() as session:
                    row = await repos.get_binding_by_channel(session, channel)
                    assert row is not None
                    row.rank_ladder_id = target.id if target is not None else None
        finally:
            await db.dispose()

        if target is None:
            typer.echo(f"channel {channel}: hierarchy unpinned, any guild ladder may apply")
        else:
            typer.echo(f"channel {channel}: hierarchy pinned to ladder {target.key!r}")

    asyncio.run(run())


@app.command("quota-set")
def quota_set(
    channel: Annotated[int, typer.Option(help="Report channel id")],
    tier: Annotated[str, typer.Option(help="Tier key from the quota ladder, e.g. instructor")],
    count: Annotated[int, typer.Option(help="Reports required per week for THIS channel")],
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Override a tier's weekly requirement for one channel.

    The number normally lives in the quota ladder file (`required_per_week`), because it
    is a property of the rank. Use this only for the exception where one channel differs;
    it affects this channel alone and leaves the ladder untouched.
    """
    settings = _settings(verbose)

    async def run() -> None:
        from sqlalchemy import select

        from dsbot.storage import ranks_repo
        from dsbot.storage.models import RankTier

        db = Database(settings)
        try:
            async with db.session() as session:
                binding = await repos.get_binding_by_channel(session, channel)
                if binding is None:
                    typer.echo(f"channel {channel} is not bound to any form")
                    raise typer.Exit(code=1)
                guild_id = binding.rank_source_guild_id or binding.guild_id
                ladders = await ranks_repo.load_all_ladders(session)
                group = ladders.get(guild_id)
                if group is None or group.quota_ladder_id is None:
                    typer.echo(
                        f"no quota ladder for guild {guild_id}. Apply one first:\n"
                        "  uv run dsbot ladder-apply --file forms/ladders/quota.yaml"
                    )
                    raise typer.Exit(code=1)
                ladder_id = group.quota_ladder_id
                tiers = (
                    await session.scalars(select(RankTier).where(RankTier.ladder_id == ladder_id))
                ).all()

            match = next((t for t in tiers if t.key == tier), None)
            if match is None:
                known = ", ".join(sorted(t.key for t in tiers))
                typer.echo(f"unknown tier {tier!r}. Known: {known}")
                raise typer.Exit(code=1)

            async with db.write_session() as session:
                await admin.set_quota(
                    session,
                    ladder_id=ladder_id,
                    tier_id=match.id,
                    binding_id=binding.id,
                    required_count=count,
                )
            typer.echo(
                f"override for channel {channel}: {match.label or match.key} -> {count}/week"
            )
            if match.required_per_week not in (None, count):
                typer.echo(
                    f"(the ladder itself says {match.required_per_week}; "
                    "that still applies to other channels)"
                )
        finally:
            await db.dispose()

    asyncio.run(run())


@app.command("quota-show")
def quota_show(
    channel: Annotated[int, typer.Option(help="Report channel id")],
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Show the weekly requirements that apply to one channel."""
    settings = _settings(verbose)

    async def run() -> None:
        from sqlalchemy import select

        from dsbot.storage import ranks_repo
        from dsbot.storage.models import RankTier

        db = Database(settings)
        try:
            async with db.session() as session:
                binding = await repos.get_binding_by_channel(session, channel)
                if binding is None:
                    typer.echo(f"channel {channel} is not bound to any form")
                    raise typer.Exit(code=1)
                guild_id = binding.rank_source_guild_id or binding.guild_id
                ladders = await ranks_repo.load_all_ladders(session)
                group = ladders.get(guild_id)
                if group is None or group.quota_ladder_id is None:
                    typer.echo(
                        f"no quota ladder for guild {guild_id}. Apply one first:\n"
                        "  uv run dsbot ladder-apply --file forms/ladders/quota.yaml"
                    )
                    raise typer.Exit(code=1)
                ladder_id = group.quota_ladder_id
                overrides = await repos.get_quota_policies(
                    session, ladder_id=ladder_id, binding_id=binding.id
                )
                tiers = (
                    await session.scalars(
                        select(RankTier)
                        .where(RankTier.ladder_id == ladder_id)
                        .order_by(RankTier.ordinal.desc())
                    )
                ).all()
        finally:
            await db.dispose()

        override_by_tier = {p.tier_id: p.required_count for p in overrides if p.tier_id}

        typer.echo(f"Weekly requirements for channel {channel} (guild {guild_id}):")
        for tier in tiers:
            label = tier.label or tier.key
            base = tier.required_per_week
            override = override_by_tier.get(tier.id)
            if override is not None and override != base:
                ladder_says = base if base is not None else "-"
                shown = f"{override}/week  (override; ladder says {ladder_says})"
            elif base is None:
                shown = "-"
            else:
                shown = f"{base}/week"
            typer.echo(f"  {tier.ordinal:>4}  {label:<24} {shown}")

        if not any(t.required_per_week is not None for t in tiers):
            typer.echo("")
            typer.echo("No tier in the ladder sets required_per_week, so nothing is demanded.")

    asyncio.run(run())


def main() -> None:
    app()
