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
            await _wait_until_ready(client, deadline_seconds=60.0)

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
) -> None:
    """Poll for READY. discord.py exposes no awaitable equivalent on Client."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + deadline_seconds
    while not client.is_ready():
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


@app.command("parse")
def parse_text(
    file: Annotated[Path, typer.Option("--file", "-f", help="Text file with a report")],
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Parse a report from a local file and print the result. Offline, no Discord."""
    import json

    _settings(verbose)

    from dsbot.modules.reports.parser import GroupMessage, ReportCandidate, parse_report
    from dsbot.modules.reports.schema import DEFAULT_SCHEMAS

    content = file.read_text(encoding="utf-8")
    group = ReportCandidate(
        messages=[
            GroupMessage(
                message_id=0,
                author_id=0,
                created_at=__import__("datetime").datetime.now(),
                content=content,
                attachment_count=0,
            )
        ]
    )
    result = parse_report(list(DEFAULT_SCHEMAS), group)
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
