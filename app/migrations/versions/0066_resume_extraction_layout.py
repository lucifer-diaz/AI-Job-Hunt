"""resume_extractions.layout, .overrides: a person's section choices rebuild the parse view without the file

* layout: each line of the stored text with its look (glyph height, weight, left edge, Word style, list item,
  columns), compact, so the parse view can be rebuilt from Resume.raw_text.
* overrides: {"<line>": section or "none"}, a person's choices for the headings of this version.

Additive. Records written before this have no layout (the view is then rebuilt from the text alone) and no
overrides. Downgrade drops both columns.

Revision ID: 0066
Revises: 0065
Create Date: 2026-11-02 09:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0066'
down_revision: str | None = '0065'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('resume_extractions') as batch:
        batch.add_column(sa.Column('layout', sa.JSON(), nullable=True))
        batch.add_column(sa.Column('overrides', sa.JSON(), nullable=False, server_default='{}'))


def downgrade() -> None:
    with op.batch_alter_table('resume_extractions') as batch:
        batch.drop_column('overrides')
        batch.drop_column('layout')
