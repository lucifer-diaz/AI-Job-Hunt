"""Add persisted non-secret application settings.

Revision ID: 20260908_01
Revises: None
"""

from alembic import op
import sqlalchemy as sa

revision = "20260908_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if "application_settings" not in inspector.get_table_names():
        op.create_table(
            "application_settings",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("active_provider", sa.String(30), nullable=False),
            sa.Column("openrouter_model", sa.String(300), nullable=False),
            sa.Column("openai_model", sa.String(300), nullable=False),
            sa.Column("anthropic_model", sa.String(300), nullable=False),
            sa.Column("approval_mode", sa.String(30), nullable=False),
            sa.Column("min_fit_score", sa.Integer(), nullable=False),
            sa.Column("followup_days", sa.Integer(), nullable=False),
            sa.Column("max_jobs_per_batch", sa.Integer(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )


def downgrade():
    op.drop_table("application_settings")
