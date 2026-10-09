"""One resume selection: the active version. Reconcile the mirrors that used to drift from it.

Before this, an upload, edited master text or a deleted library file could each change which resume the AI used,
and the profile's master text, the library's "primary" file and the active version could disagree. From now on the
active version (resumes.is_active) is the only selection (app.services.resume_verification.set_active). Per profile:

* master text that differs from the active version is kept as an inactive version ("Recovered edited text"), so
  nothing is lost; it is not made active, because the person's active version is what they chose;
* a profile with versions but none active gets its newest one;
* the master text becomes the active version's text, and the library marks only that version's file as active;
* is_default (a second selection nothing read) is cleared.

Revision ID: 0036
Revises: 0035
Create Date: 2026-10-08 10:00:00
"""
import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = '0036'
down_revision: str | None = '0035'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _hash(text: str) -> str:
    return hashlib.sha256((text or '').encode('utf-8')).hexdigest()


def upgrade() -> None:
    connection = op.get_bind()
    now = datetime.now(UTC).replace(tzinfo=None)
    resumes = sa.table('resumes', *[sa.column(name) for name in (
        'user_id', 'candidate_profile_id', 'name', 'version', 'source', 'raw_text_hash', 'raw_text', 'revision',
        'is_active', 'is_default', 'verification_status', 'created_at', 'updated_at')])
    connection.execute(sa.text('UPDATE resumes SET is_default = :no'), {'no': False})
    profiles = connection.execute(sa.text(
        'SELECT id, user_id, master_resume FROM candidate_profiles WHERE user_id IS NOT NULL ORDER BY id')).all()
    for profile in profiles:
        versions = connection.execute(sa.text(
            'SELECT id, version, raw_text, raw_text_hash, is_active, resume_document_id FROM resumes'
            ' WHERE candidate_profile_id = :profile ORDER BY version DESC'), {'profile': profile.id}).all()
        if not versions:
            continue  # a legacy profile gets its first version on first use, made active then
        active = next((v for v in versions if v.is_active), None)
        if active is None:
            active = versions[0]
            connection.execute(sa.text('UPDATE resumes SET is_active = :yes WHERE id = :id'),
                               {'yes': True, 'id': active.id})
        master = profile.master_resume or ''
        if master.strip() and master != (active.raw_text or ''):
            digest = _hash(master)
            if not any(v.raw_text_hash == digest for v in versions):
                connection.execute(resumes.insert().values(
                    user_id=profile.user_id, candidate_profile_id=profile.id, name='Recovered edited text',
                    version=versions[0].version + 1, source='manual_text', raw_text_hash=digest, raw_text=master,
                    revision=0, is_active=False, is_default=False, verification_status='UNVERIFIED',
                    created_at=now, updated_at=now))
        connection.execute(sa.text('UPDATE candidate_profiles SET master_resume = :text WHERE id = :id'),
                           {'text': active.raw_text or '', 'id': profile.id})
        connection.execute(sa.text('UPDATE resume_documents SET active = :no WHERE user_id = :user'),
                           {'no': False, 'user': profile.user_id})
        if active.resume_document_id is not None:
            connection.execute(sa.text('UPDATE resume_documents SET active = :yes WHERE id = :id'),
                               {'yes': True, 'id': active.resume_document_id})


def downgrade() -> None:
    # Data only: the reconciled values are valid under the old rules too, and the recovered version is kept.
    pass
