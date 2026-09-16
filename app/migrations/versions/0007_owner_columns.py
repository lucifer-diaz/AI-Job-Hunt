"""every user-owned row names its owner: user_id on the job and application children, and NOT NULL

Applications, tailored resumes, follow-ups, application events and workflow history used to be owned
only through their job, which meant queries had to reach the owner through a join. They now carry
user_id themselves, backfilled from their job (or their application's job).

Rows that still have no owner are given to the first admin account. A column is only made NOT NULL
when nothing in that table is unowned, so a pre-accounts install can still upgrade before anyone has
signed up; app.db_migrations.enforce_ownership tightens it after the first account claims the data.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-16 16:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0007'
down_revision: str | None = '0006'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# child table -> (foreign key column, parent table). Parents are filled in before their children.
CHILD_TABLES = (
    ('applications', 'job_id', 'jobs'),
    ('resume_variants', 'job_id', 'jobs'),
    ('workflow_transitions', 'job_id', 'jobs'),
    ('followups', 'application_id', 'applications'),
    ('application_events', 'application_id', 'applications'),
)
OWNED_TABLES = ('candidate_profiles', 'app_settings', 'resume_documents', 'knowledge_answers', 'evidence', 'jobs',
                'search_runs', 'search_profiles')
ALL_OWNED = OWNED_TABLES + tuple(table for table, _, _ in CHILD_TABLES)


def upgrade() -> None:
    bind = op.get_bind()
    for table, fk, _parent in CHILD_TABLES:
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column('user_id', sa.Integer(), nullable=True))
            batch.create_foreign_key(f'fk_{table}_user_id', 'users', ['user_id'], ['id'], ondelete='CASCADE')
        op.create_index(f'ix_{table}_user_id', table, ['user_id'])

    for table, fk, parent in CHILD_TABLES:
        op.execute(f"UPDATE {table} SET user_id = (SELECT p.user_id FROM {parent} p WHERE p.id = {table}.{fk})"
                   f" WHERE user_id IS NULL")

    owner = bind.scalar(sa.text("SELECT id FROM users ORDER BY is_admin DESC, id LIMIT 1"))
    if owner is not None:
        for table in ALL_OWNED:
            bind.execute(sa.text(f"UPDATE {table} SET user_id = :owner WHERE user_id IS NULL"), {"owner": owner})

    for table in ALL_OWNED:
        if bind.scalar(sa.text(f"SELECT COUNT(*) FROM {table} WHERE user_id IS NULL")) == 0:
            with op.batch_alter_table(table) as batch:
                batch.alter_column('user_id', existing_type=sa.Integer(), nullable=False)


def downgrade() -> None:
    for table in OWNED_TABLES:
        with op.batch_alter_table(table) as batch:
            batch.alter_column('user_id', existing_type=sa.Integer(), nullable=True)
    for table, _fk, _parent in CHILD_TABLES:
        op.drop_index(f'ix_{table}_user_id', table_name=table)
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(f'fk_{table}_user_id', type_='foreignkey')
            batch.drop_column('user_id')
