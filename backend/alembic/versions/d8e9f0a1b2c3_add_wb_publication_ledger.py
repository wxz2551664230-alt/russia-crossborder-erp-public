"""add WB publication ledger

Revision ID: d8e9f0a1b2c3
Revises: c7d8e9f0a1b2
"""

from alembic import op
import sqlalchemy as sa
import json
import uuid
from datetime import datetime, timezone


revision = "d8e9f0a1b2c3"
down_revision = "c7d8e9f0a1b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "wb_publication_items",
        sa.Column("id", sa.String(32), nullable=False),
        sa.Column("store_id", sa.String(32), nullable=False),
        sa.Column("product_id", sa.String(32), nullable=False),
        sa.Column("variant_id", sa.String(32), nullable=False),
        sa.Column("job_id", sa.String(32), nullable=True),
        sa.Column("template_id", sa.String(32), nullable=False, server_default=""),
        sa.Column("offer_id", sa.String(200), nullable=False),
        sa.Column("mode", sa.String(20), nullable=False, server_default="excel"),
        sa.Column("status", sa.String(50), nullable=False, server_default="queued"),
        sa.Column("external_product_id", sa.String(200), nullable=False, server_default=""),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["store_id"], ["stores.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["product_id"], ["catalog_products.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["variant_id"], ["catalog_variants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("store_id", "variant_id", name="uq_wb_publication_store_variant"),
        sa.UniqueConstraint("store_id", "offer_id", name="uq_wb_publication_store_offer"),
    )
    op.create_index("idx_wb_publication_product_store", "wb_publication_items", ["product_id", "store_id", "status"])
    op.create_index("idx_wb_publication_job", "wb_publication_items", ["job_id"])
    connection = op.get_bind()
    jobs = connection.execute(sa.text("SELECT id, status, payload_json, created_at, updated_at FROM jobs WHERE type='pod_wb_excel_export'"))
    for job in jobs.mappings():
        try:
            payload = json.loads(job["payload_json"] or "{}")
        except (TypeError, ValueError):
            continue
        store_id = str(payload.get("store_id") or "")
        product_ids = [str(value) for value in payload.get("product_ids") or [] if value]
        variant_ids = [str(value) for value in payload.get("variant_ids") or [] if value]
        if not store_id or not product_ids:
            continue
        if variant_ids:
            variants = connection.execute(sa.text("SELECT id, product_id, sku FROM catalog_variants WHERE id IN :ids").bindparams(sa.bindparam("ids", expanding=True)), {"ids": variant_ids}).mappings()
        else:
            variants = connection.execute(sa.text("SELECT id, product_id, sku FROM catalog_variants WHERE active=1 AND product_id IN :ids").bindparams(sa.bindparam("ids", expanding=True)), {"ids": product_ids}).mappings()
        prefix = connection.execute(sa.text("SELECT offer_prefix FROM stores WHERE id=:id"), {"id": store_id}).scalar() or ""
        ledger_status = "exported_pending_confirmation" if job["status"] == "completed" else (job["status"] if job["status"] in {"queued", "running", "failed", "cancelled"} else "failed")
        now = datetime.now(timezone.utc)
        for variant in variants:
            offer_id = "-".join(value for value in (str(prefix).strip("-"), str(variant["sku"])) if value)
            connection.execute(sa.text("""INSERT OR IGNORE INTO wb_publication_items
                (id,store_id,product_id,variant_id,job_id,template_id,offer_id,mode,status,external_product_id,last_error,confirmed_at,created_at,updated_at)
                VALUES (:id,:store_id,:product_id,:variant_id,:job_id,:template_id,:offer_id,'excel',:status,'','',NULL,:created_at,:updated_at)"""), {
                "id": uuid.uuid4().hex, "store_id": store_id, "product_id": variant["product_id"], "variant_id": variant["id"], "job_id": job["id"],
                "template_id": str(payload.get("template_id") or ""), "offer_id": offer_id, "status": ledger_status,
                "created_at": job["created_at"] or now, "updated_at": job["updated_at"] or now,
            })


def downgrade() -> None:
    op.drop_index("idx_wb_publication_job", table_name="wb_publication_items")
    op.drop_index("idx_wb_publication_product_store", table_name="wb_publication_items")
    op.drop_table("wb_publication_items")
