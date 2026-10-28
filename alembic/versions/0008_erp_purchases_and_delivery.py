"""
0008_erp_purchases_and_delivery

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-28 08:35:00.000000

Adds ERP Accounts Payable (Purchases & Supplier Debt) and Delivery & Cash-in-Transit tables:
- suppliers
- purchase_orders
- purchase_order_items
- purchase_invoices
- purchase_invoice_items
- supplier_payments
- delivery_drivers
- delivery_trips
- delivery_trip_stops
- cash_transit_handovers

Enforces Row-Level Security (RLS) on all tables for app role.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # 1. suppliers
    op.create_table(
        "suppliers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("contact_person", sa.String(128), nullable=True),
        sa.Column("phone", sa.String(50), nullable=True),
        sa.Column("bank_name", sa.String(64), nullable=True),
        sa.Column("bank_account", sa.String(64), nullable=True),
        sa.Column("payment_terms_days", sa.Integer(), nullable=False, server_default=sa.text("30")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("tenant_id", "code", name="uq_suppliers_tenant_code"),
    )
    op.create_index("ix_suppliers_tenant_id", "suppliers", ["tenant_id"])

    # 2. purchase_orders
    op.create_table(
        "purchase_orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("supplier_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("suppliers.id"), nullable=False),
        sa.Column("warehouse_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("warehouses.id"), nullable=False),
        sa.Column("po_number", sa.String(64), nullable=False),
        sa.Column("state", sa.String(32), nullable=False, server_default=sa.text("'draft'")),
        sa.Column("total_santim", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("issued_date", sa.Date(), nullable=False),
        sa.Column("expected_delivery_date", sa.Date(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("tenant_id", "po_number", name="uq_purchase_orders_tenant_po"),
        sa.CheckConstraint("total_santim >= 0", name="ck_po_total_non_negative"),
    )
    op.create_index("ix_purchase_orders_tenant_id", "purchase_orders", ["tenant_id"])
    op.create_index("ix_purchase_orders_supplier_id", "purchase_orders", ["supplier_id"])
    op.create_index("ix_purchase_orders_warehouse_id", "purchase_orders", ["warehouse_id"])

    # 3. purchase_order_items
    op.create_table(
        "purchase_order_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("purchase_order_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("purchase_orders.id"), nullable=False),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("quantity_ordered", sa.Integer(), nullable=False),
        sa.Column("quantity_received", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("unit_cost_santim", sa.BigInteger(), nullable=False),
        sa.Column("total_cost_santim", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("quantity_ordered > 0", name="ck_poi_quantity_ordered_positive"),
        sa.CheckConstraint("quantity_received >= 0", name="ck_poi_quantity_received_non_negative"),
        sa.CheckConstraint("unit_cost_santim >= 0", name="ck_poi_unit_cost_non_negative"),
        sa.CheckConstraint("total_cost_santim >= 0", name="ck_poi_total_cost_non_negative"),
    )
    op.create_index("ix_purchase_order_items_tenant_id", "purchase_order_items", ["tenant_id"])
    op.create_index("ix_purchase_order_items_po_id", "purchase_order_items", ["purchase_order_id"])
    op.create_index("ix_purchase_order_items_product_id", "purchase_order_items", ["product_id"])

    # 4. purchase_invoices
    op.create_table(
        "purchase_invoices",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("supplier_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("suppliers.id"), nullable=False),
        sa.Column("purchase_order_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("purchase_orders.id"), nullable=True),
        sa.Column("invoice_number", sa.String(64), nullable=False),
        sa.Column("amount_santim", sa.BigInteger(), nullable=False),
        sa.Column("paid_santim", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("state", sa.String(32), nullable=False, server_default=sa.text("'open'")),
        sa.Column("issued_date", sa.Date(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("tenant_id", "supplier_id", "invoice_number", name="uq_pi_tenant_supp_invoice"),
        sa.CheckConstraint("amount_santim > 0", name="ck_pi_amount_positive"),
        sa.CheckConstraint("paid_santim >= 0", name="ck_pi_paid_non_negative"),
    )
    op.create_index("ix_purchase_invoices_tenant_id", "purchase_invoices", ["tenant_id"])
    op.create_index("ix_purchase_invoices_supplier_id", "purchase_invoices", ["supplier_id"])
    op.create_index("ix_purchase_invoices_po_id", "purchase_invoices", ["purchase_order_id"])

    # 5. purchase_invoice_items
    op.create_table(
        "purchase_invoice_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("purchase_invoice_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("purchase_invoices.id"), nullable=False),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_cost_santim", sa.BigInteger(), nullable=False),
        sa.Column("total_cost_santim", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("quantity > 0", name="ck_pii_quantity_positive"),
        sa.CheckConstraint("unit_cost_santim >= 0", name="ck_pii_unit_cost_non_negative"),
        sa.CheckConstraint("total_cost_santim >= 0", name="ck_pii_total_cost_non_negative"),
    )
    op.create_index("ix_purchase_invoice_items_tenant_id", "purchase_invoice_items", ["tenant_id"])
    op.create_index("ix_purchase_invoice_items_pi_id", "purchase_invoice_items", ["purchase_invoice_id"])
    op.create_index("ix_purchase_invoice_items_product_id", "purchase_invoice_items", ["product_id"])

    # 6. supplier_payments
    op.create_table(
        "supplier_payments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("supplier_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("suppliers.id"), nullable=False),
        sa.Column("purchase_invoice_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("purchase_invoices.id"), nullable=False),
        sa.Column("amount_santim", sa.BigInteger(), nullable=False),
        sa.Column("payment_method", sa.String(32), nullable=False, server_default=sa.text("'bank_transfer'")),
        sa.Column("payment_reference", sa.String(64), nullable=True),
        sa.Column("payment_date", sa.Date(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("amount_santim > 0", name="ck_sp_amount_positive"),
    )
    op.create_index("ix_supplier_payments_tenant_id", "supplier_payments", ["tenant_id"])
    op.create_index("ix_supplier_payments_supplier_id", "supplier_payments", ["supplier_id"])
    op.create_index("ix_supplier_payments_pi_id", "supplier_payments", ["purchase_invoice_id"])

    # 7. delivery_drivers
    op.create_table(
        "delivery_drivers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("phone", sa.String(50), nullable=True),
        sa.Column("vehicle_plate", sa.String(64), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_delivery_drivers_tenant_id", "delivery_drivers", ["tenant_id"])

    # 8. delivery_trips
    op.create_table(
        "delivery_trips",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("trip_number", sa.String(64), nullable=False),
        sa.Column("driver_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("delivery_drivers.id"), nullable=False),
        sa.Column("warehouse_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("warehouses.id"), nullable=False),
        sa.Column("state", sa.String(32), nullable=False, server_default=sa.text("'planned'")),
        sa.Column("departure_time", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("completion_time", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("tenant_id", "trip_number", name="uq_delivery_trips_tenant_trip_num"),
    )
    op.create_index("ix_delivery_trips_tenant_id", "delivery_trips", ["tenant_id"])
    op.create_index("ix_delivery_trips_driver_id", "delivery_trips", ["driver_id"])
    op.create_index("ix_delivery_trips_warehouse_id", "delivery_trips", ["warehouse_id"])

    # 9. delivery_trip_stops
    op.create_table(
        "delivery_trip_stops",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("trip_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("delivery_trips.id"), nullable=False),
        sa.Column("invoice_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("invoices.id"), nullable=False),
        sa.Column("stop_order", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("status", sa.String(32), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("cash_collected_santim", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("delivery_notes", sa.Text(), nullable=True),
        sa.Column("delivered_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("cash_collected_santim >= 0", name="ck_dts_cash_collected_non_negative"),
    )
    op.create_index("ix_delivery_trip_stops_tenant_id", "delivery_trip_stops", ["tenant_id"])
    op.create_index("ix_delivery_trip_stops_trip_id", "delivery_trip_stops", ["trip_id"])
    op.create_index("ix_delivery_trip_stops_invoice_id", "delivery_trip_stops", ["invoice_id"])

    # 10. cash_transit_handovers
    op.create_table(
        "cash_transit_handovers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("trip_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("delivery_trips.id"), nullable=False),
        sa.Column("driver_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("delivery_drivers.id"), nullable=False),
        sa.Column("custodian_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("expected_cash_santim", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("actual_cash_santim", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("discrepancy_santim", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("status", sa.String(32), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("driver_confirmation_code", sa.String(16), nullable=True),
        sa.Column("handover_notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("tenant_id", "trip_id", name="uq_cth_tenant_trip"),
        sa.CheckConstraint("expected_cash_santim >= 0", name="ck_cth_expected_cash_non_negative"),
        sa.CheckConstraint("actual_cash_santim >= 0", name="ck_cth_actual_cash_non_negative"),
    )
    op.create_index("ix_cash_transit_handovers_tenant_id", "cash_transit_handovers", ["tenant_id"])
    op.create_index("ix_cash_transit_handovers_trip_id", "cash_transit_handovers", ["trip_id"])
    op.create_index("ix_cash_transit_handovers_driver_id", "cash_transit_handovers", ["driver_id"])

    # 11. Enable and Force Row-Level Security on all new tables
    tables = [
        "suppliers",
        "purchase_orders",
        "purchase_order_items",
        "purchase_invoices",
        "purchase_invoice_items",
        "supplier_payments",
        "delivery_drivers",
        "delivery_trips",
        "delivery_trip_stops",
        "cash_transit_handovers",
    ]

    for tbl in tables:
        conn.execute(sa.text(f"""
            ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY;
            ALTER TABLE {tbl} FORCE ROW LEVEL SECURITY;

            CREATE POLICY {tbl}_tenant_isolation ON {tbl}
                FOR ALL
                TO app
                USING (
                    tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid
                )
                WITH CHECK (
                    tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid
                );

            GRANT SELECT, INSERT, UPDATE, DELETE ON {tbl} TO app;
            GRANT SELECT ON {tbl} TO backup_role;
        """))


def downgrade() -> None:
    tables = [
        "cash_transit_handovers",
        "delivery_trip_stops",
        "delivery_trips",
        "delivery_drivers",
        "supplier_payments",
        "purchase_invoice_items",
        "purchase_invoices",
        "purchase_order_items",
        "purchase_orders",
        "suppliers",
    ]
    for tbl in tables:
        op.drop_table(tbl)
