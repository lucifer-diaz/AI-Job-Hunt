"""Add durable, verified ATS integration knowledge records."""

from alembic import op
import sqlalchemy as sa

revision = "0006_ats_knowledge_records"
down_revision = "0005_application_execution_state"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ats_knowledge_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ats_name", sa.String(100), nullable=False),
        sa.Column("record_key", sa.String(200), nullable=False),
        sa.Column("knowledge", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("source_url", sa.String(1200), nullable=False, server_default=""),
        sa.Column("verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()
        ),
        sa.UniqueConstraint("ats_name", "record_key", name="uq_ats_knowledge_name_key"),
    )


def downgrade():
    op.drop_table("ats_knowledge_records")
