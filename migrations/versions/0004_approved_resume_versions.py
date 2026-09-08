"""Add immutable approved resume snapshots and backfill approved applications."""

from alembic import op
import sqlalchemy as sa

revision = "0004_approved_resume_versions"
down_revision = "0003_resume_documents"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "approved_resume_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "application_id",
            sa.Integer(),
            sa.ForeignKey("applications.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "resume_variant_id",
            sa.Integer(),
            sa.ForeignKey("resume_variants.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("content", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("markdown", sa.Text(), nullable=False, server_default=""),
        sa.Column("cover_letter", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "approved_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()
        ),
        sa.UniqueConstraint("application_id"),
    )
    op.create_index(
        "ix_approved_resume_versions_application_id",
        "approved_resume_versions",
        ["application_id"],
        unique=True,
    )
    op.create_index(
        "ix_approved_resume_versions_resume_variant_id",
        "approved_resume_versions",
        ["resume_variant_id"],
    )
    conn = op.get_bind()
    applications = sa.table(
        "applications",
        sa.column("id"),
        sa.column("job_id"),
        sa.column("status"),
        sa.column("approved_at"),
        sa.column("created_at"),
    )
    resumes = sa.table(
        "resume_variants",
        sa.column("id"),
        sa.column("job_id"),
        sa.column("version"),
        sa.column("content"),
        sa.column("markdown"),
        sa.column("cover_letter"),
    )
    approved = sa.table(
        "approved_resume_versions",
        sa.column("application_id"),
        sa.column("resume_variant_id"),
        sa.column("content"),
        sa.column("markdown"),
        sa.column("cover_letter"),
        sa.column("approved_at"),
    )
    for app in conn.execute(
        sa.select(applications).where(applications.c.status.in_(["approved", "applied"]))
    ):
        resume = (
            conn.execute(
                sa.select(resumes)
                .where(resumes.c.job_id == app.job_id)
                .order_by(resumes.c.version.desc(), resumes.c.id.desc())
                .limit(1)
            )
            .mappings()
            .first()
        )
        if resume:
            conn.execute(
                approved.insert().values(
                    application_id=app.id,
                    resume_variant_id=resume.id,
                    content=resume.content or {},
                    markdown=resume.markdown or "",
                    cover_letter=resume.cover_letter or "",
                    approved_at=app.approved_at or app.created_at,
                )
            )


def downgrade():
    op.drop_index(
        "ix_approved_resume_versions_resume_variant_id", table_name="approved_resume_versions"
    )
    op.drop_index(
        "ix_approved_resume_versions_application_id", table_name="approved_resume_versions"
    )
    op.drop_table("approved_resume_versions")
