"""Structured logging. Console output is ASCII-safe on Windows (cp866 consoles)."""

from __future__ import annotations

import contextlib
import logging
import sys

import structlog


def configure_console_encoding() -> None:
    """Force UTF-8 on stdout/stderr.

    Windows consoles default to cp866/cp1251 here, which mangles Russian report text
    and template strings. Reporters paste Cyrillic constantly, so this is not optional.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(ValueError, OSError):
                reconfigure(encoding="utf-8", errors="replace")


def configure_logging(level: str = "INFO", fmt: str = "console") -> None:
    configure_console_encoding()
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)

    shared: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]

    renderer: structlog.types.Processor
    if fmt == "json":
        renderer = structlog.processors.JSONRenderer(ensure_ascii=False)
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=False)

    structlog.configure(
        processors=[*shared, structlog.processors.format_exc_info, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    # discord.py is chatty at INFO during backfill.
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if level == "DEBUG" else logging.WARNING
    )
