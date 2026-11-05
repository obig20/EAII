"""
app/kernel/events.py

Append-only event log.

Rules enforced here:
  Rule 4: Every state change writes an event. No UPDATE or DELETE on events.
           Corrections are new compensating events.

The only public write function is `append_event`.  There is intentionally
no `update_event` or `delete_event` function in this module.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.kernel.enums import EvidenceLevel
from app.kernel.models import Event


# ---------------------------------------------------------------------------
# Public API — append only
# ---------------------------------------------------------------------------
def append_event(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    event_type: str,
    aggregate_type: str,
    aggregate_id: uuid.UUID,
    payload: dict[str, Any],
    actor_id: uuid.UUID | None = None,
    evidence_level: EvidenceLevel | None = None,
) -> Event:
    """
    Write a new event to the append-only event log.

    Args:
        session:        An active SQLAlchemy Session with tenant context set.
        tenant_id:      The tenant this event belongs to.
        event_type:     Dot-namespaced event name, e.g. "invoice.created".
        aggregate_type: The entity type, e.g. "invoice", "payment_line".
        aggregate_id:   UUID of the affected entity.
        payload:        JSON-serialisable dict.  Must NOT contain PII at INFO+
                        log level — callers are responsible for filtering.
        actor_id:       UUID of the user who triggered the change (None for
                        system events such as auto-matchers).
        evidence_level: Present when the event relates to a payment evidence
                        transition.

    Returns:
        The persisted Event ORM object (flushed, not yet committed).
    """
    event = Event(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        actor_id=actor_id,
        evidence_level=evidence_level.value if evidence_level is not None else None,
        payload=payload,
        created_at=datetime.now(timezone.utc),
    )
    session.add(event)
    session.flush()  # write to DB within current transaction; caller commits
    return event


def compensating_event(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    original_event_id: uuid.UUID,
    event_type: str,
    aggregate_type: str,
    aggregate_id: uuid.UUID,
    payload: dict[str, Any],
    actor_id: uuid.UUID | None = None,
    reason: str,
) -> Event:
    """
    Write a compensating (correction) event.

    The original event is NEVER modified.  This new event references the
    original via `compensates_event_id` in the payload.

    Convention: event_type should be "<original_type>.compensated", e.g.
    "invoice.created.compensated".
    """
    full_payload = {
        "compensates_event_id": str(original_event_id),
        "reason": reason,
        **payload,
    }
    return append_event(
        session,
        tenant_id=tenant_id,
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        payload=full_payload,
        actor_id=actor_id,
    )
