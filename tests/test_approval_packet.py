"""The approved packet and the canonical state at approval (app.services.approval, Phase 4).

Approval moves the job to READY_TO_APPLY through validated transitions backed by stored facts, or is refused with
nothing written; it keeps an immutable snapshot and digest of what was approved; an edit afterwards revokes the
approval in the same transaction; export, packet issuance and the apply queue use the packet only while it matches.
These tests run the production rule (`strict_approval`).
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db import SessionLocal
from app.main import app
from app.models import Application, ApprovedPacket, Job, ResumeVariant, WorkflowTransition
from app.services import apply_queue, approval
from app.services.lifecycle import WorkflowState as S
from app.services.workflow import approve_job

pytestmark = pytest.mark.strict_approval
POSTING = ("Lead Python platform teams building services on Kubernetes and AWS. You will own the roadmap for our data "
           "platform, mentor five engineers, and work with product to ship reliably. We look for experience with "
           "FastAPI, distributed systems and a track record of leading delivery.")
client = TestClient(app)


def _user() -> int:
    from tests.conftest import ensure_test_user

    return ensure_test_user().id


def job_with_packet(state: S = S.QUALIFIED, *, score: float = 90, status: str = "tailored") -> tuple[int, int]:
    """A job in `state` with one reviewed tailored version. Returns (job id, variant id)."""
    user = _user()
    with SessionLocal() as db:
        db.info["user_id"] = user
        job = Job(user_id=user, source="test", source_ref=uuid.uuid4().hex, title="Engineering Manager",
                  company=f"Globex {uuid.uuid4().hex[:6]}", description="Lead Python teams.",
                  url=f"https://jobs.example.com/{uuid.uuid4().hex}", status=status, state=state)
        db.add(job)
        db.flush()
        variant = ResumeVariant(user_id=user, job_id=job.id, version=1, reviewer_score=score,
                                markdown="# Asha Rao\r\n- Led eight engineers\n", cover_letter="Dear team,\n\nHello.",
                                content={"summary": "", "experience": []}, reviewer_notes={"recommendation": "PASS"})
        db.add(variant)
        db.commit()
        return job.id, variant.id


def _counts(job_id: int) -> tuple[int, int, int]:
    with SessionLocal() as db:
        return (db.scalar(select(func.count()).select_from(Application).where(Application.job_id == job_id)),
                db.scalar(select(func.count()).select_from(ApprovedPacket).where(ApprovedPacket.job_id == job_id)),
                db.scalar(select(func.count()).select_from(WorkflowTransition)
                          .where(WorkflowTransition.job_id == job_id)))


def _history(job_id: int) -> list[tuple[str, str, str]]:
    with SessionLocal() as db:
        return [(t.from_state.value, t.to_state.value, t.actor) for t in db.scalars(
            select(WorkflowTransition).where(WorkflowTransition.job_id == job_id).order_by(WorkflowTransition.id))]


def test_the_digest_is_canonical_and_deterministic():
    content = {"resume_markdown": "a\nb", "cover_letter": "c", "attachments": [{"kind": "resume", "variant_id": 1,
                                                                                "variant_version": 1}]}
    assert approval.digest_of(content) == approval.digest_of(dict(reversed(list(content.items()))))
    assert approval.digest_of(content) != approval.digest_of({**content, "cover_letter": "c "})
    assert b'"serializer_version":1' in approval.canonical(content)


def test_approval_moves_a_qualified_job_through_its_real_stages():
    job_id, variant_id = job_with_packet(S.QUALIFIED)
    assert client.post(f"/api/jobs/{job_id}/approve").status_code == 200
    assert _history(job_id) == [("QUALIFIED", "TAILORING", "reconcile"), ("TAILORING", "TAILORED", "reconcile"),
                                ("TAILORED", "REVIEWED", "reconcile"), ("REVIEWED", "READY_TO_APPLY", "approval")]
    with SessionLocal() as db:
        packet = db.scalar(select(ApprovedPacket).where(ApprovedPacket.variant_id == variant_id, ApprovedPacket.job_id == job_id)
                           .order_by(ApprovedPacket.id.desc()).limit(1))
        assert packet.job_id == job_id and packet.revoked_at is None
        assert packet.content["resume_markdown"] == "# Asha Rao\n- Led eight engineers"  # newlines normalised
        assert packet.digest == approval.digest_of(packet.content)


@pytest.mark.parametrize("state", [S.RANK_PENDING, S.ARCHIVED, S.SKIPPED, S.DISCOVERED])
def test_approval_is_refused_with_nothing_written_when_no_path_exists(state):
    job_id, _ = job_with_packet(state)
    before = _counts(job_id)
    response = client.post(f"/api/jobs/{job_id}/approve")
    assert response.status_code == 409
    assert "ranked again" in response.json()["detail"] if state is S.RANK_PENDING else "Re-rank" in \
        response.json()["detail"]
    assert _counts(job_id) == before
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        assert job.status == "tailored" and job.state is state


def test_an_edit_after_approval_revokes_it_in_the_same_transaction():
    job_id, variant_id = job_with_packet(S.QUALIFIED)
    client.post(f"/api/jobs/{job_id}/approve")
    assert client.put(f"/api/resumes/{variant_id}/cover-letter", json={"text": "A new letter"}).status_code == 200
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        packet = db.scalar(select(ApprovedPacket).where(ApprovedPacket.variant_id == variant_id, ApprovedPacket.job_id == job_id)
                           .order_by(ApprovedPacket.id.desc()).limit(1))
        assert job.status == "pending_approval" and job.application.status == "pending_approval"
        assert job.state is S.REVIEWED
        assert packet.revoked_at is not None and "edited after approval" in packet.revoked_reason
        # What was approved stays exactly as it was.
        assert packet.content["cover_letter"] == "Dear team,\n\nHello."
    assert client.post(f"/api/jobs/{job_id}/approve").status_code == 200  # approve the edited packet again
    with SessionLocal() as db:
        rows = db.scalars(select(ApprovedPacket).where(ApprovedPacket.variant_id == variant_id, ApprovedPacket.job_id == job_id)
                          .order_by(ApprovedPacket.id)).all()[-2:]  # SQLite may keep a deleted job's rows (ids reused)
        assert [r.revoked_at is None for r in rows] == [False, True]
        assert rows[-1].content["cover_letter"] == "A new letter"
        assert db.get(Job, job_id).state is S.READY_TO_APPLY


def test_sending_an_approved_packet_back_withdraws_the_approval():
    job_id, _ = job_with_packet(S.QUALIFIED)
    assert client.post(f"/api/jobs/{job_id}/approve").status_code == 200
    assert client.post(f"/api/jobs/{job_id}/revision", json={"notes": "Shorter summary"}).status_code == 200
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        packet = db.scalar(select(ApprovedPacket).where(ApprovedPacket.job_id == job_id)
                           .order_by(ApprovedPacket.id.desc()).limit(1))
        assert job.status == "pending_approval" and job.application.status == "pending_approval"
        assert job.state is S.REVIEWED, "the canonical state leaves READY_TO_APPLY with the approval"
        assert packet.revoked_at is not None and packet.revoked_reason == "Sent back for revision"
        assert approval.live(db, job.application) is None
    assert _history(job_id)[-1] == ("READY_TO_APPLY", "REVIEWED", approval.ACTOR)


def test_rejecting_an_approved_job_withdraws_its_snapshot():
    job_id, _ = job_with_packet(S.QUALIFIED)
    assert client.post(f"/api/jobs/{job_id}/approve").status_code == 200
    assert client.post(f"/api/jobs/{job_id}/reject", json={"reason": "", "notes": ""}).status_code == 200
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        packet = db.scalar(select(ApprovedPacket).where(ApprovedPacket.job_id == job_id)
                           .order_by(ApprovedPacket.id.desc()).limit(1))
        assert job.status == "dismissed" and job.state is S.SKIPPED
        assert packet.revoked_at is not None and approval.live(db, job.application) is None


def test_a_packet_changed_behind_the_approval_is_refused_for_export_and_application():
    job_id, variant_id = job_with_packet(S.QUALIFIED)
    client.post(f"/api/jobs/{job_id}/approve")
    assert client.get(f"/api/jobs/{job_id}/cover-letter.txt").status_code == 200
    with SessionLocal() as db:  # a write that went around the edit endpoints
        db.get(ResumeVariant, variant_id).markdown = "# Someone else"
        db.commit()
    for path in ("resume.pdf", "resume.docx", "cover-letter.txt"):
        response = client.get(f"/api/jobs/{job_id}/{path}")
        assert response.status_code == 409 and "approve it again" in response.json()["detail"]
    with SessionLocal() as db:
        db.info["user_id"] = _user()
        job = db.get(Job, job_id)
        assert "approve it again" in apply_queue.not_ready_reason(db, job)
        assert approval.verify(db, job.application).ok is False


def test_a_submitted_packet_cannot_be_edited():
    job_id, variant_id = job_with_packet(S.QUALIFIED)
    client.post(f"/api/jobs/{job_id}/approve")
    with SessionLocal() as db:
        app_row = db.scalar(select(Application).where(Application.job_id == job_id))
        app_row.status = "applied"
        db.commit()
    assert client.put(f"/api/resumes/{variant_id}/cover-letter", json={"text": "x"}).status_code == 409


def test_a_legacy_approval_without_a_snapshot_keeps_working():
    job_id, variant_id = job_with_packet(S.QUALIFIED)
    with SessionLocal() as db:
        db.info["user_id"] = _user()
        db.add(Application(job_id=job_id, status="approved", approved_resume_id=variant_id))
        db.commit()
        application = db.scalar(select(Application).where(Application.job_id == job_id))
        assert approval.verify(db, application).reason == "LEGACY"


def test_a_new_tailoring_pass_after_a_revoked_approval_is_recorded_before_approving_again():
    job_id, variant_id = job_with_packet(S.QUALIFIED)
    client.post(f"/api/jobs/{job_id}/approve")
    client.put(f"/api/resumes/{variant_id}/cover-letter", json={"text": "Edited"})
    with SessionLocal() as db:
        db.info["user_id"] = _user()
        db.add(ResumeVariant(user_id=_user(), job_id=job_id, version=2, reviewer_score=91, markdown="# v2",
                             cover_letter="", content={}, reviewer_notes={}))
        db.commit()
        approve_job(db, db.get(Job, job_id))
    tail = _history(job_id)[-4:]
    assert tail == [("REVIEWED", "TAILORING", "reconcile"), ("TAILORING", "TAILORED", "reconcile"),
                    ("TAILORED", "REVIEWED", "reconcile"), ("REVIEWED", "READY_TO_APPLY", "approval")]


# ───────────── a job held by a failed ranking: the way back (ranker.pipeline_status, POST /api/jobs/{id}/rank) ─────────────

def failed_ranking_job() -> tuple[int, int]:
    """A packet tailored and reviewed, whose later re-ranking (a profile change) failed its attempts."""
    job_id, variant_id = job_with_packet(S.FAILED, status="pending_approval")
    with SessionLocal() as db:
        from sqlalchemy.orm.attributes import set_committed_value

        job = db.get(Job, job_id)
        set_committed_value(job, "previous_state", S.RANK_PENDING)
        db.query(Job).filter(Job.id == job_id).update({"previous_state": S.RANK_PENDING, "fit_score": 84,
                                                       "description": POSTING})
        db.add(WorkflowTransition(job_id=job_id, user_id=job.user_id, from_state=S.RANK_PENDING, to_state=S.FAILED,
                                  actor="ranker", reason="Ranking gave up after 3 attempt(s): EXTERNAL_SERVICE"))
        db.commit()
    return job_id, variant_id


def test_a_failed_ranking_is_explained_on_the_job_with_the_way_back():
    job_id, _ = failed_ranking_job()
    pipeline = client.get(f"/api/jobs/{job_id}").json()["pipeline"]
    assert pipeline["state"] == "FAILED" and pipeline["stage"] == "ranking" and pipeline["action"] == "rank"
    assert pipeline["action_label"] == "Put back in ranking" and pipeline["approval_blocked"] is True
    assert "gave up after 3 attempt" in pipeline["reason"]
    assert client.get(f"/api/approvals/{job_id}").json()["pipeline"]["action"] == "rank"
    refused = client.post(f"/api/jobs/{job_id}/approve")
    assert refused.status_code == 409 and "Put back in ranking" in refused.json()["detail"]


def test_putting_it_back_requeues_the_ranking_and_approval_opens_once_it_qualifies():
    from app.models import JobRankingTask
    from app.services.work_queue import transition

    job_id, _ = failed_ranking_job()
    out = client.post(f"/api/jobs/{job_id}/rank")
    assert out.status_code == 202 and out.json()["queued"] == [job_id]
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        assert job.state is S.RANK_PENDING and job.attempt_count == 0 and job.status == "pending_approval"
        assert db.scalar(select(func.count()).select_from(JobRankingTask)
                         .where(JobRankingTask.job_id == job_id, JobRankingTask.status == "PENDING")) == 1
    pipeline = client.get(f"/api/jobs/{job_id}").json()["pipeline"]
    assert pipeline["title"] == "Being ranked" and pipeline["action"] is None
    with SessionLocal() as db:  # the ranker's own moves when the new ranking qualifies it
        job = db.get(Job, job_id)
        for target in (S.RANKING, S.RANKED, S.QUALIFIED):
            transition(db, job, target, actor="ranker", reason="test")
        db.commit()
    assert client.get(f"/api/jobs/{job_id}").json()["pipeline"] is None
    assert client.post(f"/api/jobs/{job_id}/approve").status_code == 200
    with SessionLocal() as db:
        assert db.get(Job, job_id).state is S.READY_TO_APPLY


def test_a_job_archived_by_ranking_offers_rank_again():
    job_id, _ = job_with_packet(S.ARCHIVED, status="pending_approval")
    with SessionLocal() as db:
        db.query(Job).filter(Job.id == job_id).update({"previous_state": S.RANKED, "description": POSTING})
        db.commit()
    pipeline = client.get(f"/api/jobs/{job_id}").json()["pipeline"]
    assert pipeline["action"] == "rank" and pipeline["action_label"] == "Rank again"
    assert "Rank again" in client.post(f"/api/jobs/{job_id}/approve").json()["detail"]


def test_a_held_job_without_a_description_is_sent_to_add_one():
    job_id, _ = job_with_packet(S.FAILED, status="pending_approval")
    with SessionLocal() as db:
        db.query(Job).filter(Job.id == job_id).update({"previous_state": S.RANK_PENDING, "description": ""})
        db.commit()
    pipeline = client.get(f"/api/jobs/{job_id}").json()["pipeline"]
    assert pipeline["action"] == "description" and pipeline["action_label"] == "Add description"
