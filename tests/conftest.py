"""
tests/conftest.py

Shared pytest fixtures.

The test DB is expected to be running and accessible via the DATABASE_URL
environment variable.  In CI this is the db_test container from
docker-compose.test.yml.  Locally, set DATABASE_URL to a test database.

Design note on RLS + test sessions
------------------------------------
RLS is FORCED, meaning even the 'app' DB role is subject to it.  The
DATABASE_URL used in tests must point to the SUPERUSER (postgres) account so
that:
  a) create_all / drop_all can run DDL including RLS policy creation.
  b) Seed fixtures (tenant_a, tenant_b, owner_user_a) can INSERT without
     first setting app.current_tenant — the superuser bypasses RLS.
  c) RLS tests then simulate an app-role session by calling set_tenant()
     *within the same transaction*, after the seed rows exist.

In docker-compose.test.yml DATABASE_URL is set to the superuser URL.
In production the app connects as the 'app' role (never superuser).

Fixtures:
  - engine        : SQLAlchemy engine (session-scoped)
  - tables        : creates all tables once per session, drops at end
  - db_session    : a fresh session per test, rolled back after each test
  - tenant_a      : a Tenant row for tenant isolation tests
  - tenant_b      : a second Tenant row for tenant isolation tests
  - owner_user_a  : a User in tenant_a with role=owner
"""

from __future__ import annotations

import os
import uuid

# Set test defaults for test execution if not explicitly provided
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://postgres@localhost:5432/dailyclose_test")
os.environ.setdefault("SECRET_KEY", "test-secret-key-32-chars-long-minimum!")
os.environ.setdefault("APP_ENV", "test")
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.kernel.models import Base, Tenant, User


# ---------------------------------------------------------------------------
# Engine — one per test session (superuser URL for DDL + seed)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def engine():
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.fail(
            "DATABASE_URL environment variable is not set. "
            "Run tests via docker-compose.test.yml or set DATABASE_URL."
        )
    eng = create_engine(url, pool_pre_ping=True, echo=False)
    yield eng
    eng.dispose()


# ---------------------------------------------------------------------------
# Tables — create once per session using SQLAlchemy metadata
# For Stage 1 we use create_all (mirrors models).
# The actual Alembic migration is integration-tested separately.
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def tables(engine):
    if engine.dialect.name == "postgresql":
        import pathlib
        from alembic import command
        from alembic.config import Config

        ini_path = pathlib.Path(__file__).parent.parent / "alembic.ini"
        alembic_cfg = Config(str(ini_path))
        alembic_cfg.set_main_option("sqlalchemy.url", str(engine.url))
        command.upgrade(alembic_cfg, "head")
        yield
        command.downgrade(alembic_cfg, "base")
    else:
        Base.metadata.create_all(engine)
        yield
        Base.metadata.drop_all(engine)


# ---------------------------------------------------------------------------
# Per-test session — rolled back after each test for isolation
# ---------------------------------------------------------------------------
@pytest.fixture
def db_session(engine, tables):
    """
    Yield a SQLAlchemy Session that is automatically rolled back after the
    test.  The session connects as the superuser (DATABASE_URL in tests),
    which bypasses RLS — this is intentional for seed fixtures.
    RLS tests call set_tenant() explicitly to simulate app-role behaviour.
    """
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)

    yield session

    session.close()
    transaction.rollback()
    connection.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Seed fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def tenant_a(db_session: Session) -> Tenant:
    t = Tenant(id=uuid.uuid4(), name="Tenant Alpha", created_at=_now())
    db_session.add(t)
    db_session.flush()
    return t


@pytest.fixture
def tenant_b(db_session: Session) -> Tenant:
    t = Tenant(id=uuid.uuid4(), name="Tenant Beta", created_at=_now())
    db_session.add(t)
    db_session.flush()
    return t


@pytest.fixture
def owner_user_a(db_session: Session, tenant_a: Tenant) -> User:
    u = User(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        role="owner",
        display_name="Alpha Owner",
        created_at=_now(),
    )
    db_session.add(u)
    db_session.flush()
    return u
