"""Engine/session lifecycle."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from forgesmith.config import Settings
from forgesmith.config import settings as default_settings
from forgesmith.db.models import Base

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _ensure_parent_dir(url: str) -> None:
    if not url.startswith("sqlite"):
        return
    _, _, tail = url.partition("///")
    if not tail or tail == ":memory:":
        return
    Path(tail).parent.mkdir(parents=True, exist_ok=True)


def get_engine(settings: Settings | None = None) -> AsyncEngine:
    global _engine
    if _engine is None:
        cfg = settings or default_settings
        _ensure_parent_dir(cfg.database_url)
        _engine = create_async_engine(cfg.database_url, future=True)
    return _engine


def get_session_factory(settings: Settings | None = None) -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(settings), expire_on_commit=False)
    return _session_factory


async def init_db(settings: Settings | None = None) -> None:
    """Create tables if missing. Alembic takes over when we move to Postgres."""
    engine = get_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def dispose_db() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


@asynccontextmanager
async def session_scope(
    settings: Settings | None = None,
) -> AsyncIterator[AsyncSession]:
    factory = get_session_factory(settings)
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def reset_state() -> None:
    """Test helper: forget the cached engine so a new URL takes effect."""
    global _engine, _session_factory
    _engine = None
    _session_factory = None
