"""Async SQLAlchemy engine/session management."""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core import tenancy  # noqa: F401  (registers the RLS after_begin listener)
from app.core.config import Settings


class Database:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.sessionmaker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    @classmethod
    def from_settings(cls, settings: Settings, *, application_name: str = "claimos-api") -> Database:
        engine = create_async_engine(
            settings.database_url.get_secret_value(),
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=settings.db_pool_timeout_seconds,
            pool_pre_ping=True,
            pool_recycle=1800,
            connect_args={
                "timeout": settings.db_connect_timeout_seconds,
                "server_settings": {"application_name": application_name},
            },
        )
        return cls(engine)

    async def ping(self) -> None:
        async with self.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    async def current_revision(self) -> str | None:
        """Return the applied Alembic revision, or None when migrations have never run."""
        async with self.engine.connect() as conn:
            try:
                result = await conn.execute(text("SELECT version_num FROM alembic_version"))
            except ProgrammingError:
                return None
            row = result.first()
            return str(row[0]) if row else None

    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessionmaker() as session:
            yield session

    async def dispose(self) -> None:
        await self.engine.dispose()
