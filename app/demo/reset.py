"""
app/demo/reset.py

Safe reset logic for Daily Close demo tenants.
Ensures zero data contamination and provides strict safety guards
preventing non-demo / production tenants from being wiped.
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.demo.generator import seed_demo_data
from app.kernel.enums import UserRole
from app.kernel.events import append_event
from app.kernel.models import (
    ImportBatch,
    Invoice,
    PaymentLine,
    PaymentMatch,
    ProcessedApprovalToken,
    Retailer,
    RetailerConfirmationToken,
    ReversalNote,
    Tenant,
    User,
)
from app.kernel.tenant import set_tenant


def reset_demo_tenant(session: Session, tenant_id: uuid.UUID) -> None:
    """
    Safely reset a demo tenant back to its clean baseline state.
    Strict Safety Guard:
      Asserts that tenant.is_demo is True. Raises PermissionError otherwise.
    """
    tenant = session.get(Tenant, tenant_id)
    if not tenant:
        raise ValueError(f"Tenant {tenant_id} not found.")

    # 1. HARD SAFETY GUARD: Never allow resetting a production tenant!
    if not tenant.is_demo:
        raise PermissionError(
            f"Safety Violation: Tenant {tenant.id} ('{tenant.name}') is NOT a demo tenant. Reset aborted."
        )

    set_tenant(session, tenant.id)

    # 2. Find owner user to associate with fresh seed data
    owner = session.execute(
        select(User).where(
            User.tenant_id == tenant.id,
            User.role == UserRole.OWNER.value,
        )
    ).scalars().first()

    if not owner:
        # Fallback to any active user in tenant
        owner = session.execute(
            select(User).where(User.tenant_id == tenant.id)
        ).scalars().first()

    if not owner:
        raise ValueError(f"No user found for demo tenant {tenant.id}.")

    # 3. Cascade delete child domain entities within tenant scope
    # (Ordered by foreign key dependencies: matches -> lines -> batches, notes, tokens, invoices, retailers)
    session.execute(
        delete(ReversalNote).where(ReversalNote.tenant_id == tenant.id)
    )
    session.execute(
        delete(ProcessedApprovalToken).where(ProcessedApprovalToken.tenant_id == tenant.id)
    )
    session.execute(
        delete(PaymentMatch).where(PaymentMatch.tenant_id == tenant.id)
    )
    session.execute(
        delete(PaymentLine).where(PaymentLine.tenant_id == tenant.id)
    )
    session.execute(
        delete(ImportBatch).where(ImportBatch.tenant_id == tenant.id)
    )
    session.execute(
        delete(RetailerConfirmationToken).where(RetailerConfirmationToken.tenant_id == tenant.id)
    )
    session.execute(
        delete(Invoice).where(Invoice.tenant_id == tenant.id)
    )
    session.execute(
        delete(Retailer).where(Retailer.tenant_id == tenant.id)
    )
    session.flush()

    # 4. Re-seed fresh Addis wholesale baseline data
    seed_demo_data(session, tenant, owner)

    # 5. Record immutable demo reset event
    append_event(
        session,
        tenant_id=tenant.id,
        event_type="demo.reset",
        aggregate_type="tenant",
        aggregate_id=tenant.id,
        actor_id=owner.id,
        payload={"reset_by": str(owner.id)},
    )

    session.commit()
