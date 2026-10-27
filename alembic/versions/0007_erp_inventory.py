"""
0007_erp_inventory

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-27 23:55:00.000000

Adds ERP Inventory & Multi-Warehouse tables:
- warehouses
- product_categories
- products
- stock_levels
- stock_movements
- invoice_items
Enforces Row-Level Security (RLS) on all tables for app role.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # 1. warehouses
    op.create_table(
        "warehouses",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("location", sa.String(255), nullable=True),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("tenant_id", "code", name="uq_warehouses_tenant_code"),
    )
    op.create_index("ix_warehouses_tenant_id", "warehouses", ["tenant_id"])

    # 2. product_categories
    op.create_table(
        "product_categories",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("code", sa.String(32), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("tenant_id", "name", name="uq_product_categories_tenant_name"),
    )
    op.create_index("ix_product_categories_tenant_id", "product_categories", ["tenant_id"])

    # 3. products
    op.create_table(
        "products",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("category_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("product_categories.id"), nullable=True),
        sa.Column("sku", sa.String(64), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("unit_of_measure", sa.String(32), nullable=False, server_default=sa.text("'pcs'")),
        sa.Column("unit_price_santim", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("cost_price_santim", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("reorder_threshold", sa.Integer(), nullable=False, server_default=sa.text("10")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("tenant_id", "sku", name="uq_products_tenant_sku"),
        sa.CheckConstraint("unit_price_santim >= 0", name="ck_product_unit_price_positive"),
        sa.CheckConstraint("cost_price_santim >= 0", name="ck_product_cost_price_positive"),
    )
    op.create_index("ix_products_tenant_id", "products", ["tenant_id"])
    op.create_index("ix_products_category_id", "products", ["category_id"])

    # 4. stock_levels
    op.create_table(
        "stock_levels",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("warehouse_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("warehouses.id"), nullable=False),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("quantity_on_hand", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("quantity_reserved", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("tenant_id", "warehouse_id", "product_id", name="uq_stock_levels_wh_product"),
        sa.CheckConstraint("quantity_on_hand >= 0", name="ck_stock_qty_non_negative"),
    )
    op.create_index("ix_stock_levels_tenant_id", "stock_levels", ["tenant_id"])
    op.create_index("ix_stock_levels_warehouse_id", "stock_levels", ["warehouse_id"])
    op.create_index("ix_stock_levels_product_id", "stock_levels", ["product_id"])

    # 5. stock_movements
    op.create_table(
        "stock_movements",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("movement_type", sa.String(32), nullable=False),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("source_warehouse_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("warehouses.id"), nullable=True),
        sa.Column("destination_warehouse_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("warehouses.id"), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_cost_santim", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("reference", sa.String(64), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("quantity > 0", name="ck_stock_movement_quantity_positive"),
        sa.CheckConstraint("unit_cost_santim >= 0", name="ck_stock_movement_unit_cost_non_negative"),
    )
    op.create_index("ix_stock_movements_tenant_id", "stock_movements", ["tenant_id"])
    op.create_index("ix_stock_movements_product_id", "stock_movements", ["product_id"])
    op.create_index("ix_stock_movements_source_wh", "stock_movements", ["source_warehouse_id"])
    op.create_index("ix_stock_movements_dest_wh", "stock_movements", ["destination_warehouse_id"])

    # 6. invoice_items
    op.create_table(
        "invoice_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("invoice_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("invoices.id"), nullable=False),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("warehouse_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("warehouses.id"), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price_santim", sa.BigInteger(), nullable=False),
        sa.Column("total_price_santim", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("quantity > 0", name="ck_invoice_item_quantity_positive"),
        sa.CheckConstraint("unit_price_santim >= 0", name="ck_invoice_item_unit_price_non_negative"),
        sa.CheckConstraint("total_price_santim >= 0", name="ck_invoice_item_total_price_non_negative"),
    )
    op.create_index("ix_invoice_items_tenant_id", "invoice_items", ["tenant_id"])
    op.create_index("ix_invoice_items_invoice_id", "invoice_items", ["invoice_id"])
    op.create_index("ix_invoice_items_product_id", "invoice_items", ["product_id"])

    # 7. Enable and Force Row-Level Security on all new tables
    tables = [
        "warehouses",
        "product_categories",
        "products",
        "stock_levels",
        "stock_movements",
        "invoice_items",
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
        "invoice_items",
        "stock_movements",
        "stock_levels",
        "products",
        "product_categories",
        "warehouses",
    ]
    for tbl in tables:
        op.drop_table(tbl)
