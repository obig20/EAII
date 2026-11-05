"""
tests/kernel/test_events.py

Tests for Rule 4: Append-only event log.

Verifies:
  - append_event writes a new row (SELECT succeeds)
  - UPDATE on events raises ProgrammingError (DB-level REVOKE)
  - DELETE on events raises ProgrammingError (DB-level REVOKE)
  - compensating_event writes a new row referencing the original
  - No update_event or delete_event function exists in events module

These tests require a real PostgreSQL database with the migration applied.
They are skipped if DATABASE_URL is not set.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from app.kernel import events as events_module
from app.kernel.enums import EvidenceLevel
from app.kernel.events import append_event, compensating_event
from app.kernel.models import Event, Tenant


# ---------------------------------------------------------------------------
# Skip DB-dependent classes if no DB available (pure-unit test environments)
# ---------------------------------------------------------------------------
requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB-dependent event tests",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# append_event
# ---------------------------------------------------------------------------
@requires_db
class TestAppendEvent:
    def test_append_creates_row(self, db_session, tenant_a):
        agg_id = uuid.uuid4()
        event = append_event(
            db_session,
            tenant_id=tenant_a.id,
            event_type="invoice.created",
            aggregate_type="invoice",
            aggregate_id=agg_id,
            payload={"invoice_number": "INV001", "amount_santim": 10000},
        )
        db_session.flush()

        # Verify the row exists
        result = db_session.execute(
            text("SELECT id, event_type FROM events WHERE id = :eid"),
            {"eid": str(event.id)},
        ).fetchone()

        assert result is not None
        assert result[1] == "invoice.created"

    def test_append_with_evidence_level(self, db_session, tenant_a):
        event = append_event(
            db_session,
            tenant_id=tenant_a.id,
            event_type="payment.confirmed",
            aggregate_type="payment_line",
            aggregate_id=uuid.uuid4(),
            payload={"state": "confirmed"},
            evidence_level=EvidenceLevel.L2,
        )
        db_session.flush()
        assert event.evidence_level == "L2"

    def test_append_with_actor(self, db_session, tenant_a, owner_user_a):
        actor_id = owner_user_a.id
        event = append_event(
            db_session,
            tenant_id=tenant_a.id,
            event_type="invoice.voided",
            aggregate_type="invoice",
            aggregate_id=uuid.uuid4(),
            payload={"reason": "data entry error"},
            actor_id=actor_id,
        )
        db_session.flush()
        assert event.actor_id == actor_id

    def test_multiple_appends_accumulate(self, db_session, tenant_a):
        agg_id = uuid.uuid4()
        e1 = append_event(
            db_session,
            tenant_id=tenant_a.id,
            event_type="invoice.created",
            aggregate_type="invoice",
            aggregate_id=agg_id,
            payload={"seq": 1},
        )
        e2 = append_event(
            db_session,
            tenant_id=tenant_a.id,
            event_type="invoice.state_changed",
            aggregate_type="invoice",
            aggregate_id=agg_id,
            payload={"seq": 2, "new_state": "partially_paid"},
        )
        db_session.flush()
        assert e1.id != e2.id

        rows = db_session.execute(
            text("SELECT COUNT(*) FROM events WHERE aggregate_id = :aid"),
            {"aid": str(agg_id)},
        ).scalar()
        assert rows == 2


# ---------------------------------------------------------------------------
# Append-only enforcement at DB level
# ---------------------------------------------------------------------------
@requires_db
class TestAppendOnlyAtDbLevel:
    def test_update_on_events_raises_permission_error(self, db_session, tenant_a):
        """
        Rule 4: The app DB role must not be able to UPDATE events.
        REVOKE UPDATE ON events FROM app is applied in the migration.
        """
        agg_id = uuid.uuid4()
        append_event(
            db_session,
            tenant_id=tenant_a.id,
            event_type="invoice.created",
            aggregate_type="invoice",
            aggregate_id=agg_id,
            payload={"original": True},
        )
        db_session.flush()

        db_session.execute(text("SET ROLE app"))
        with pytest.raises(ProgrammingError, match="permission denied"):
            db_session.execute(
                text("UPDATE events SET event_type = 'tampered' WHERE aggregate_id = :aid"),
                {"aid": str(agg_id)},
            )

    def test_delete_on_events_raises_permission_error(self, db_session, tenant_a):
        """
        Rule 4: The app DB role must not be able to DELETE events.
        """
        agg_id = uuid.uuid4()
        append_event(
            db_session,
            tenant_id=tenant_a.id,
            event_type="invoice.created",
            aggregate_type="invoice",
            aggregate_id=agg_id,
            payload={"original": True},
        )
        db_session.flush()

        db_session.execute(text("SET ROLE app"))
        with pytest.raises(ProgrammingError, match="permission denied"):
            db_session.execute(
                text("DELETE FROM events WHERE aggregate_id = :aid"),
                {"aid": str(agg_id)},
            )


# ---------------------------------------------------------------------------
# Compensating event pattern
# ---------------------------------------------------------------------------
@requires_db
class TestCompensatingEvent:
    def test_compensating_event_references_original(self, db_session, tenant_a):
        agg_id = uuid.uuid4()
        original = append_event(
            db_session,
            tenant_id=tenant_a.id,
            event_type="invoice.created",
            aggregate_type="invoice",
            aggregate_id=agg_id,
            payload={"invoice_number": "INV999"},
        )
        db_session.flush()

        comp = compensating_event(
            db_session,
            tenant_id=tenant_a.id,
            original_event_id=original.id,
            event_type="invoice.created.compensated",
            aggregate_type="invoice",
            aggregate_id=agg_id,
            payload={"invoice_number": "INV999_CORRECTED"},
            reason="Wrong invoice number entered",
        )
        db_session.flush()

        assert comp.id != original.id
        assert comp.event_type == "invoice.created.compensated"
        assert str(original.id) in comp.payload["compensates_event_id"]
        assert comp.payload["reason"] == "Wrong invoice number entered"

    def test_original_event_still_exists_after_compensation(self, db_session, tenant_a):
        """The original event is NEVER deleted or modified."""
        agg_id = uuid.uuid4()
        original = append_event(
            db_session,
            tenant_id=tenant_a.id,
            event_type="invoice.created",
            aggregate_type="invoice",
            aggregate_id=agg_id,
            payload={"data": "original"},
        )
        db_session.flush()

        compensating_event(
            db_session,
            tenant_id=tenant_a.id,
            original_event_id=original.id,
            event_type="invoice.created.compensated",
            aggregate_type="invoice",
            aggregate_id=agg_id,
            payload={"data": "correction"},
            reason="test",
        )
        db_session.flush()

        # Original row still exists and is unchanged
        orig_row = db_session.execute(
            text("SELECT event_type, payload FROM events WHERE id = :eid"),
            {"eid": str(original.id)},
        ).fetchone()
        assert orig_row is not None
        assert orig_row[0] == "invoice.created"
        assert orig_row[1]["data"] == "original"


# ---------------------------------------------------------------------------
# No mutation API exists in the events module
# ---------------------------------------------------------------------------
class TestNoMutationApiExists:
    def test_no_update_event_function(self):
        """Rule 4: There must be no update_event function in the events module."""
        assert not hasattr(events_module, "update_event"), (
            "events module must NOT expose update_event — append-only rule violated"
        )

    def test_no_delete_event_function(self):
        """Rule 4: There must be no delete_event function in the events module."""
        assert not hasattr(events_module, "delete_event"), (
            "events module must NOT expose delete_event — append-only rule violated"
        )

    def test_append_event_exists(self):
        assert hasattr(events_module, "append_event")

    def test_compensating_event_exists(self):
        assert hasattr(events_module, "compensating_event")
