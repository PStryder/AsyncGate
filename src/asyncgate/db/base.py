"""Database connection and session management."""

import time
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from asyncgate.config import settings
from asyncgate.observability.metrics import metrics


class Base(DeclarativeBase):
    """SQLAlchemy declarative base."""

    pass


# Create async engine
engine = create_async_engine(
    settings.database_url,
    echo=settings.debug,
    pool_pre_ping=True,
    pool_size=20,
    max_overflow=10,
    connect_args={"ssl": False},  # Disable SSL for Fly.io internal network
)


def _attach_query_metrics(target_engine) -> None:
    """Attach SQLAlchemy event listeners for query metrics."""
    sync_engine = target_engine.sync_engine
    if getattr(sync_engine, "_asyncgate_metrics_attached", False):
        return

    @event.listens_for(sync_engine, "before_cursor_execute")
    def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        conn.info["query_start_time"] = time.perf_counter()

    @event.listens_for(sync_engine, "after_cursor_execute")
    def after_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        start_time = conn.info.pop("query_start_time", None)
        if start_time is None:
            return
        duration_ms = (time.perf_counter() - start_time) * 1000.0
        metrics.inc_counter("db.query.count")
        metrics.observe("db.query.duration_ms", duration_ms)

    sync_engine._asyncgate_metrics_attached = True

# Create async session factory
async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

# Attach metrics to default engine
_attach_query_metrics(engine)


async def init_db() -> None:
    """Initialize database tables."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_db() -> None:
    """Close database connections."""
    await engine.dispose()


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Get a database session."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
