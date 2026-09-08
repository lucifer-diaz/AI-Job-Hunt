"""Baseline for the schema that predates Alembic."""

from alembic import op
import sqlalchemy as sa

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "candidate_profiles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("email", sa.String(300), nullable=False),
        sa.Column("phone", sa.String(100), nullable=False),
        sa.Column("location", sa.String(200), nullable=False),
        sa.Column("linkedin_url", sa.String(500), nullable=False),
        sa.Column("github_url", sa.String(500), nullable=False),
        sa.Column("headline", sa.String(500), nullable=False),
        sa.Column("target_roles", sa.JSON(), nullable=False),
        sa.Column("preferred_locations", sa.JSON(), nullable=False),
        sa.Column("work_authorization", sa.Text(), nullable=False),
        sa.Column("compensation_notes", sa.Text(), nullable=False),
        sa.Column("master_resume", sa.Text(), nullable=False),
        sa.Column("search_preferences", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "evidence",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company", sa.String(200), nullable=False),
        sa.Column("role", sa.String(200), nullable=False),
        sa.Column("claim", sa.Text(), nullable=False),
        sa.Column("context", sa.Text(), nullable=False),
        sa.Column("skills", sa.JSON(), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("verified", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("source_ref", sa.String(500), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("company", sa.String(300), nullable=False),
        sa.Column("location", sa.String(300), nullable=False),
        sa.Column("url", sa.String(1200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("raw", sa.JSON(), nullable=False),
        sa.Column("discovered_at", sa.DateTime(), nullable=False),
        sa.Column("fit_score", sa.Float(), nullable=True),
        sa.Column("fit_analysis", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(50), nullable=False),
        sa.UniqueConstraint("source", "source_ref", name="uq_job_source_ref"),
    )
    op.create_table(
        "search_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("external_run_id", sa.String(200), nullable=False),
        sa.Column("actor_id", sa.String(300), nullable=False),
        sa.Column("input_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(50), nullable=False),
        sa.Column("dataset_id", sa.String(200), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("created_jobs", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("external_run_id"),
    )
    op.create_index(
        "ix_search_runs_external_run_id", "search_runs", ["external_run_id"], unique=True
    )
    op.create_table(
        "resume_variants",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "job_id", sa.Integer(), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("markdown", sa.Text(), nullable=False),
        sa.Column("cover_letter", sa.Text(), nullable=False),
        sa.Column("reviewer_score", sa.Float(), nullable=True),
        sa.Column("reviewer_notes", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_resume_variants_job_id", "resume_variants", ["job_id"])
    op.create_table(
        "applications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "job_id",
            sa.Integer(),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("status", sa.String(50), nullable=False),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("applied_at", sa.DateTime(), nullable=True),
        sa.Column("followup_due_at", sa.DateTime(), nullable=True),
        sa.Column("recruiter_name", sa.String(200), nullable=False),
        sa.Column("recruiter_email", sa.String(300), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "followups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "application_id",
            sa.Integer(),
            sa.ForeignKey("applications.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("due_at", sa.DateTime(), nullable=False),
        sa.Column("channel", sa.String(30), nullable=False),
        sa.Column("draft", sa.Text(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_followups_application_id", "followups", ["application_id"])


def downgrade():
    op.drop_index("ix_followups_application_id", table_name="followups")
    op.drop_table("followups")
    op.drop_table("applications")
    op.drop_index("ix_resume_variants_job_id", table_name="resume_variants")
    op.drop_table("resume_variants")
    op.drop_index("ix_search_runs_external_run_id", table_name="search_runs")
    op.drop_table("search_runs")
    op.drop_table("jobs")
    op.drop_table("evidence")
    op.drop_table("candidate_profiles")
