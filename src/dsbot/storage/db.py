"""Engine, session factory, and the serialized write path.

SQLite permits exactly one writer. Rather than discovering that under load as
`database is locked`, every write goes through a single async lock (and a busy
timeout as a second belt). Reads are unrestricted. When this moves to Postgres the
lock becomes a no-op and nothing above this file changes.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, TypeVar

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from dsbot.config import Settings
from dsbot.storage.models import Base

T = TypeVar("T")

_BUSY_TIMEOUT_MS = 15_000


class Database:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._engine: AsyncEngine = self._build_engine(settings)
        self._session_factory = async_sessionmaker(
            self._engine, expire_on_commit=False, class_=AsyncSession
        )
        self._write_lock = asyncio.Lock()

    @staticmethod
    def _build_engine(settings: Settings) -> AsyncEngine:
        url = settings.resolved_database_url
        if url.startswith("sqlite"):
            _ensure_sqlite_dir(url)
        engine = create_async_engine(
            url,
            echo=settings.sql_echo,
            pool_pre_ping=True,
            future=True,
        )
        if url.startswith("sqlite"):

            @event.listens_for(engine.sync_engine, "connect")
            def _sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
                cursor = dbapi_conn.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
                cursor.close()

        return engine

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Read-only-ish session. Callers must not rely on implicit writes."""
        async with self._session_factory() as session:
            yield session

    @asynccontextmanager
    async def write_session(self) -> AsyncIterator[AsyncSession]:
        """Serialized write transaction, committed on clean exit."""
        async with self._write_lock, self._session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def run_write(self, fn: Callable[[AsyncSession], Awaitable[T]]) -> T:
        async with self.write_session() as session:
            return await fn(session)

    async def create_all(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def prepare(self) -> list[str]:
        """Create missing tables, then add missing columns to existing ones.

        `create_all` alone is not enough for a live deployment: it never alters a table
        that already exists, so a column added by a new release would be invisible and
        every query mentioning it would fail. Upgrades are additive, so this is safe to
        call on every start and on every CLI command that reads report data.
        """
        from dsbot.storage.migrate import upgrade_schema

        await self.create_all()
        return await upgrade_schema(self)

    async def healthcheck(self) -> bool:
        try:
            async with self.session() as session:
                await session.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    async def dispose(self) -> None:
        await self._engine.dispose()


def _ensure_sqlite_dir(url: str) -> None:
    """Create the parent directory for a file-backed SQLite URL."""
    if ":memory:" in url:
        return
    raw = url.split("///", 1)[-1] if "///" in url else ""
    if not raw:
        return
    path = Path(raw)
    if path.suffix or path.name:
        path.parent.mkdir(parents=True, exist_ok=True)
