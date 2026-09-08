"""Add source resume documents and backfill the master resume."""

from alembic import op
import sqlalchemy as sa

revision = "0003_resume_documents"
down_revision = "0002_settings"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "resume_documents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "candidate_profile_id",
            sa.Integer(),
            sa.ForeignKey("candidate_profiles.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("filename", sa.String(500), nullable=False, server_default=""),
        sa.Column("content_type", sa.String(200), nullable=False, server_default="text/plain"),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()
        ),
    )
    op.create_index(
        "ix_resume_documents_candidate_profile_id", "resume_documents", ["candidate_profile_id"]
    )
    profiles = sa.table(
        "candidate_profiles", sa.column("id", sa.Integer), sa.column("master_resume", sa.Text)
    )
    docs = sa.table(
        "resume_documents",
        sa.column("candidate_profile_id", sa.Integer),
        sa.column("filename", sa.String),
        sa.column("content_type", sa.String),
        sa.column("content", sa.Text),
        sa.column("metadata", sa.JSON),
        sa.column("is_active", sa.Boolean),
    )
    conn = op.get_bind()
    for row in conn.execute(
        sa.select(profiles.c.id, profiles.c.master_resume).where(profiles.c.master_resume != "")
    ):
        conn.execute(
            docs.insert().values(
                candidate_profile_id=row.id,
                filename="master-resume.txt",
                content_type="text/plain",
                content=row.master_resume,
                metadata={},
                is_active=True,
            )
        )


def downgrade():
    op.drop_index("ix_resume_documents_candidate_profile_id", table_name="resume_documents")
    op.drop_table("resume_documents")
