"""Add price-drop alert rules.

Revision ID: 0002
Revises: 0001
"""

from alembic import op
import sqlalchemy as sa


revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "alert_rules",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("market_id", sa.BigInteger(), sa.ForeignKey("markets.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("threshold", sa.Numeric(asdecimal=True), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("is_alerted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("last_alerted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("threshold > 0 AND threshold < 1", name="ck_alert_rules_threshold"),
    )


def downgrade():
    op.drop_table("alert_rules")
