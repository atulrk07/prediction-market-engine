"""Create market metadata, collection runs, and exact quote snapshots.

Revision ID: 0001
Revises: None

Keep this schema frozen; later schema changes belong in a new migration.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "markets",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("close_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("platform", "external_id", name="uq_markets_platform_external_id"),
    )
    op.create_table(
        "ingestion_runs",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("collection_slot", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'running'")),
        sa.Column("attempted_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("inserted_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("duplicate_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("watchlist_config", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("market_outcomes", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.CheckConstraint("status IN ('running', 'success', 'partial', 'failed', 'incomplete')", name="ck_runs_status"),
        sa.CheckConstraint("attempted_count >= 0 AND inserted_count >= 0 AND duplicate_count >= 0 AND failed_count >= 0", name="ck_runs_nonnegative_counts"),
        sa.CheckConstraint("finished_at IS NULL OR finished_at >= started_at", name="ck_runs_time_order"),
        sa.CheckConstraint("jsonb_typeof(watchlist_config) = 'object' AND jsonb_typeof(market_outcomes) = 'object'", name="ck_runs_json_objects"),
    )
    op.create_table(
        "snapshots",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("market_id", sa.BigInteger(), sa.ForeignKey("markets.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("ingestion_run_id", sa.BigInteger(), sa.ForeignKey("ingestion_runs.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("collection_slot", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("yes_bid", sa.Numeric(asdecimal=True), nullable=True),
        sa.Column("yes_bid_quantity", sa.Numeric(asdecimal=True), nullable=True),
        sa.Column("yes_ask", sa.Numeric(asdecimal=True), nullable=True),
        sa.Column("yes_ask_quantity", sa.Numeric(asdecimal=True), nullable=True),
        sa.UniqueConstraint("market_id", "collection_slot", name="uq_snapshots_market_slot"),
        sa.CheckConstraint("yes_bid IS NULL OR (yes_bid BETWEEN 0 AND 1 AND scale(yes_bid) <= 4)", name="ck_snapshots_bid_price"),
        sa.CheckConstraint("yes_ask IS NULL OR (yes_ask BETWEEN 0 AND 1 AND scale(yes_ask) <= 4)", name="ck_snapshots_ask_price"),
        sa.CheckConstraint("yes_bid_quantity IS NULL OR (yes_bid_quantity >= 0 AND scale(yes_bid_quantity) <= 2 AND yes_bid_quantity NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric))", name="ck_snapshots_bid_quantity"),
        sa.CheckConstraint("yes_ask_quantity IS NULL OR (yes_ask_quantity >= 0 AND scale(yes_ask_quantity) <= 2 AND yes_ask_quantity NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric))", name="ck_snapshots_ask_quantity"),
        sa.CheckConstraint("(yes_bid IS NULL) = (yes_bid_quantity IS NULL)", name="ck_snapshots_bid_pair"),
        sa.CheckConstraint("(yes_ask IS NULL) = (yes_ask_quantity IS NULL)", name="ck_snapshots_ask_pair"),
    )
    op.create_index("ix_snapshots_market_observed_id", "snapshots", ["market_id", "observed_at", "id"])


def downgrade():
    op.drop_index("ix_snapshots_market_observed_id", table_name="snapshots")
    op.drop_table("snapshots")
    op.drop_table("ingestion_runs")
    op.drop_table("markets")
