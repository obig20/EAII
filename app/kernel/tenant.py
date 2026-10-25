"""
app/kernel/tenant.py

PostgreSQL Row-Level Security (RLS) tenant context helpers.

Every DB session used by the application MUST call `set_tenant(session, tenant_id)`
before issuing any query.  This sets the `app.current_tenant` session-local
parameter, which the RLS policies on all tenant-scoped tables read.

The superuser (used only by Alembic migrations) bypasses RLS automatically;
normal app sessions CANNOT bypass it because FORCE ROW LEVEL SECURITY is
applied in the migration.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import text
from sqlalchemy.orm import Session


_UNSET = object()


def set_tenant(session: Session, tenant_id: uuid.UUID) -> None:
    """
    Set the current tenant for this database session.

    This must be called at the start of every request/task that touches
    tenant-scoped data.  Uses set_config(..., true) so the setting is
    transaction-scoped (SET LOCAL) and automatically cleared on COMMIT or ROLLBACK.

    Args:
        session:   An active SQLAlchemy Session.
        tenant_id: The UUID of the tenant whose data this session may access.
    """
    session.execute(
        text("SELECT set_config('app.current_tenant', :tid, true)"),
        {"tid": str(tenant_id)},
    )


@contextmanager
def tenant_session(
    session: Session, tenant_id: uuid.UUID
) -> Generator[Session, None, None]:
    """
    Context manager that sets the tenant and yields the session.

    Usage::

        with tenant_session(session, tenant_id) as s:
            s.add(invoice)

    The caller is still responsible for commit/rollback.
    """
    set_tenant(session, tenant_id)
    try:
        yield session
    except Exception:
        session.rollback()
        raise
