"""
app/kernel/db.py

Database engine and session factory.

The engine uses the DATABASE_URL from settings.  For migrations, Alembic uses
DATABASE_MIGRATION_URL (superuser) via alembic/env.py.

Usage in application code::

    from app.kernel.db import get_session

    with get_session() as session:
        ...

Usage in tests: override DATABASE_URL via environment before importing this
module, or use the `db_session` pytest fixture from tests/conftest.py.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from app.settings import settings


# ---------------------------------------------------------------------------
# Engine — synchronous (psycopg3 sync driver)
# ---------------------------------------------------------------------------
_engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    echo=settings.log_level == "DEBUG",
)


# ---------------------------------------------------------------------------
# Session factory
# ---------------------------------------------------------------------------
_SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False)


def get_session() -> Generator[Session, None, None]:
    """
    FastAPI dependency yielding a database session.
    Commits on clean exit, rolls back on exception, closes in finally.

    Callers MUST call `set_tenant(session, tenant_id)` before any query.
    """
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


session_scope = contextmanager(get_session)


def get_engine():
    """Return the shared SQLAlchemy engine (useful for test setup)."""
    return _engine
