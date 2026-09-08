"""Add resumable application execution state and backfill every application."""

from alembic import op
import sqlalchemy as sa

revision = "0005_application_execution_state"
down_revision = "0004_approved_resume_versions"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "application_execution_states",
        sa.Column(
            "application_id",
            sa.Integer(),
            sa.ForeignKey("applications.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("state", sa.String(50), nullable=False, server_default="not_started"),
        sa.Column("current_step", sa.String(100), nullable=False, server_default=""),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("context", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()
        ),
    )
    applications = sa.table(
        "applications", sa.column("id", sa.Integer), sa.column("status", sa.String)
    )
    states = sa.table(
        "application_execution_states",
        sa.column("application_id", sa.Integer),
        sa.column("state", sa.String),
        sa.column("current_step", sa.String),
        sa.column("attempts", sa.Integer),
        sa.column("context", sa.JSON),
        sa.column("last_error", sa.Text),
    )
    conn = op.get_bind()
    state_for = {"applied": "completed", "approved": "ready"}
    for row in conn.execute(sa.select(applications.c.id, applications.c.status)):
        conn.execute(
            states.insert().values(
                application_id=row.id,
                state=state_for.get(row.status, "not_started"),
                current_step="",
                attempts=0,
                context={},
                last_error="",
            )
        )


def downgrade():
    op.drop_table("application_execution_states")
