"""Pre-submit claims (Phase 5): one submission per posting, across MCP, the Apply kit and duplicate job rows.

`apply_queue.begin_submission` reserves every alias of a posting before anything external starts; the one recorder
(`record_submission`) checks the claim, the approved digest and duplicates; an expired claim is UNCERTAIN until a
person says what happened. SQLite for the rules; PostgreSQL (TEST_DATABASE_URL) for true concurrency.
"""
from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import auth
from app.auth import CurrentUser
from app.db import SessionLocal
from app.main import app
from app.mcp import tools
from app.mcp.server import run_tool
from app.models import Application, ApplicationClaim, ApplicationClaimKey, ApplicationEvent, Job, ResumeVariant
from app.services import apply_queue, approval
from app.settings import get_settings
from tests.conftest import make_user

RESUME = "# Asha Rao\n\n## Experience\n- Scaled the payments API with Python\n"
URL = "https://jobs.example.test/examplecorp/4012345"


def now():
    return datetime.now(UTC).replace(tzinfo=None)


def session(user_id, factory=SessionLocal):
    db = factory()
    db.info["user_id"] = user_id
    return db


@pytest.fixture(autouse=True)
def packet_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "mcp_packet_dir", str(tmp_path / "packets"))


def approved_job(user_id, *, factory=SessionLocal, snapshot=True, **fields):
    """An approved job with a reviewed version and (by default) its approval snapshot and digest."""
    with session(user_id, factory) as db:
        job = Job(user_id=user_id, source="manual", source_ref=uuid.uuid4().hex, title="Senior Backend Engineer",
                  company="Examplecorp", location="Singapore", status="approved",
                  **{"url": URL, **fields})
        db.add(job)
        db.flush()
        variant = ResumeVariant(user_id=user_id, job_id=job.id, version=1, markdown=RESUME,
                                cover_letter="Dear Examplecorp,", content={}, reviewer_score=90)
        db.add(variant)
        db.flush()
        application = Application(user_id=user_id, job_id=job.id, status="approved", approved_at=now(),
                                  approved_resume_id=variant.id)
        db.add(application)
        db.flush()
        if snapshot:
            approval.snapshot(db, application, variant)
        db.commit()
        return job.id


def begin(user_id, job_id, client="mcp", factory=SessionLocal):
    with session(user_id, factory) as db:
        result = apply_queue.begin_submission(db, db.get(Job, job_id), client=client)
        return result.status, (result.claim.token if result.claim and result.ok else None), result.conflict_job_id


def record(user_id, job_id, token=None, client="mcp"):
    with session(user_id) as db:
        result = apply_queue.record_submission(db, db.get(Job, job_id), claim_token=token, client=client)
        return result.status


@pytest.fixture
def user():
    return make_user(SessionLocal)


# ───────────── one claim per posting ─────────────

def test_a_posting_is_claimed_once_and_recorded_with_its_token(user):
    job_id = approved_job(user)
    status, token, _ = begin(user, job_id)
    assert status == "CLAIMED" and token
    assert begin(user, job_id, "apply_kit")[0] == "CLAIM_CONFLICT"  # never a second token
    assert record(user, job_id, token) == "SUBMITTED"
    assert record(user, job_id, token) == "ALREADY_APPLIED"  # idempotent
    with session(user) as db:
        claim = db.scalar(select(ApplicationClaim).where(ApplicationClaim.token == token))
        assert claim.status == "RECORDED" and all(k.held for k in claim.keys)
        event = db.scalars(select(ApplicationEvent).where(ApplicationEvent.kind == "applied")).one()
        assert event.meta["claim"] == "claimed" and event.meta["claim_id"] == claim.id


@pytest.mark.parametrize("alias", [
    {"url": "https://www.jobs.example.test/examplecorp/4012345/?utm_source=linkedin"},
    {"url": "https://board.example.test/view/1", "direct_apply_url": "http://jobs.example.test/examplecorp/4012345#x"},
    {"url": "https://board.example.test/view/2", "source_board": "greenhouse", "source_job_id": "4012345"},
])
def test_a_duplicate_job_row_or_url_alias_collides_with_the_claim(user, alias):
    first = approved_job(user, source_board="greenhouse", source_job_id="4012345")
    duplicate = approved_job(user, **alias)
    status, token, _ = begin(user, first)
    assert status == "CLAIMED"
    status, _, conflict = begin(user, duplicate, "apply_kit")
    assert (status, conflict) == ("CLAIM_CONFLICT", first)
    assert record(user, duplicate, client="apply_kit") == "CLAIM_CONFLICT"  # a tokenless report is refused too
    assert record(user, first, token) == "SUBMITTED"
    assert begin(user, duplicate)[0] == "DUPLICATE"
    assert record(user, duplicate) == "DUPLICATE_APPLICATION"


def test_a_site_root_is_not_an_alias(user):
    first = approved_job(user, url="https://careers.example.test/")
    other = approved_job(user, url="https://careers.example.test")
    assert begin(user, first)[0] == "CLAIMED" and begin(user, other)[0] == "CLAIMED"


def test_jobs_without_canonical_columns_are_detected_as_already_applied(user):
    applied = approved_job(user, url="https://jobs.example.test/a/1?utm_campaign=x")
    assert record(user, applied) == "SUBMITTED"  # a tokenless report, recorded as unclaimed
    with session(user) as db:  # an older application recorded before claims existed has no keys: drop them
        for key in db.scalars(select(ApplicationClaimKey)).all():
            db.delete(key)
        db.commit()
    alias = approved_job(user, url="https://board.example.test/v/9", direct_apply_url="https://jobs.example.test/a/1/")
    with session(user) as db:
        assert "Already applied" in apply_queue.not_ready_reason(db, db.get(Job, alias))
    assert begin(user, alias)[0] == "DUPLICATE"


def test_a_token_works_only_for_its_own_job(user):
    first = approved_job(user)
    other = approved_job(user, url="https://jobs.example.test/other/2")
    _, token, _ = begin(user, first)
    assert record(user, other, token) == "INVALID_CLAIM"
    assert record(user, first, "not-a-token") == "INVALID_CLAIM"


# ───────────── expired, uncertain, released ─────────────

def test_an_expired_claim_is_uncertain_until_a_person_resolves_it(user):
    job_id = approved_job(user)
    _, token, _ = begin(user, job_id)
    with session(user) as db:
        claim = db.scalar(select(ApplicationClaim).where(ApplicationClaim.token == token))
        claim.lease_expires_at = now() - timedelta(minutes=1)
        db.commit()
        claim_id = claim.id
    assert begin(user, job_id)[0] == "UNCERTAIN"  # never a second submission because a lease ran out
    with session(user) as db:
        assert db.get(ApplicationClaim, claim_id).status == "UNCERTAIN"
        with pytest.raises(ValueError):
            apply_queue.release_claim(db, db.get(Job, job_id), token, "client gone")  # a person's to resolve
        assert record(user, job_id, token) == "INVALID_CLAIM"
    with session(user) as db:
        released = apply_queue.resolve_uncertain(db, db.get(Job, job_id), claim_id, submitted=False)
        assert released.status == "RELEASED" and not any(k.held for k in released.keys)
    status, token, _ = begin(user, job_id)
    assert status == "CLAIMED"


def test_resolving_an_uncertain_claim_as_submitted_records_it(user):
    job_id = approved_job(user)
    _, token, _ = begin(user, job_id)
    with session(user) as db:
        assert apply_queue.expire_claims(db, now=now() + timedelta(days=1)) == 1
        claim = db.scalar(select(ApplicationClaim).where(ApplicationClaim.token == token))
        result = apply_queue.resolve_uncertain(db, db.get(Job, job_id), claim.id, submitted=True,
                                               confirmation_id="GH-1")
        assert result.status == "SUBMITTED" and result.application.confirmation_id == "GH-1"


def test_a_blocker_with_the_token_releases_the_claim(user):
    job_id = approved_job(user)
    _, token, _ = begin(user, job_id)
    with session(user) as db:
        apply_queue.record_blocker(db, db.get(Job, job_id), "captcha", claim_token=token)
    assert begin(user, job_id)[0] == "CLAIMED"


# ───────────── the approved packet ─────────────

def test_a_packet_cannot_be_edited_while_its_submission_is_open(user):
    job_id = approved_job(user)
    begin(user, job_id)
    with session(user) as db:
        variant = db.scalar(select(ResumeVariant).where(ResumeVariant.job_id == job_id))
        with pytest.raises(ValueError, match="submission of this packet has started"):
            approval.guard_edit(db, variant, "Resume")


def test_a_packet_changed_behind_the_guard_is_not_recorded(user):
    job_id = approved_job(user)
    _, token, _ = begin(user, job_id)
    with session(user) as db:
        variant = db.scalar(select(ResumeVariant).where(ResumeVariant.job_id == job_id))
        variant.markdown += "\n- changed"
        db.commit()
    assert record(user, job_id, token) == "PACKET_CHANGED"
    assert record(user, job_id) == "PACKET_CHANGED"
    assert begin(user, approved_job(user, url="https://jobs.example.test/z/3"))[0] == "CLAIMED"


# ───────────── tenancy ─────────────

def test_accounts_never_see_or_collide_with_each_others_claims(user):
    other = make_user(SessionLocal)
    mine, theirs = approved_job(user), approved_job(other)  # the same posting URL in two accounts
    _, token, _ = begin(user, mine)
    assert begin(other, theirs)[0] == "CLAIMED"
    assert record(other, theirs, token) == "INVALID_CLAIM"
    with session(other) as db:
        assert {c.job_id for c in db.scalars(select(ApplicationClaim)).all()} == {theirs}


# ───────────── MCP ─────────────

def call(user_id, name, **arguments):
    return run_tool(SessionLocal, user_id, name, getattr(tools, name), arguments)


def test_mcp_begin_release_and_record_with_the_claim(user):
    job_id = approved_job(user)
    begun = call(user, "begin_application_submission", job_id=job_id)
    assert begun["ok"] and begun["status"] == "CLAIMED" and begun["claim_token"]
    again = call(user, "begin_application_submission", job_id=job_id)
    assert not again["ok"] and again["status"] == "CLAIM_CONFLICT"
    released = call(user, "release_application_claim", job_id=job_id, claim_token=begun["claim_token"],
                    reason="The form asked for an account")
    assert released["status"] == "RELEASED"
    begun = call(user, "begin_application_submission", job_id=job_id)
    out = call(user, "record_application_result", job_id=job_id, outcome="submitted",
               claim_token=begun["claim_token"], confirmation_id="GH-77")
    assert out["ok"] and out["status"] == "SUBMITTED" and out["claimed"] is True


def test_mcp_listing_leaves_out_a_posting_someone_is_submitting(user):
    job_id = approved_job(user)
    begin(user, job_id, "apply_kit")
    listed = call(user, "list_jobs_for_application")
    assert job_id not in [j["job_id"] for j in listed["jobs"]]
    blocked = call(user, "list_jobs_for_application", include_blocked=True)
    (entry,) = [j for j in blocked["jobs"] if j["job_id"] == job_id]
    assert entry["claim"]["status"] == "CLAIMED" and entry["claim"]["client"] == "apply_kit"


# ───────────── the Apply kit ─────────────

def client_for(monkeypatch, user_id):
    monkeypatch.setattr(auth, "authenticate", lambda request: CurrentUser(user_id, f"{user_id}@jobhunt.test", False))
    return TestClient(app)


def test_the_apply_kit_starts_reports_and_releases_through_the_claim(user, monkeypatch):
    client = client_for(monkeypatch, user)
    job_id = approved_job(user)
    started = client.post(f"/api/jobs/{job_id}/submission/start")
    assert started.status_code == 200
    token = started.json()["claim"]["claim_token"]
    assert client.get(f"/api/jobs/{job_id}/apply-kit").json()["claim"]["status"] == "CLAIMED"
    assert call(user, "begin_application_submission", job_id=job_id)["status"] == "CLAIM_CONFLICT"
    refused = client.post(f"/api/jobs/{job_id}/applied", json={})  # a report without the token while it is open
    assert refused.status_code == 409 and refused.json()["detail"]["status"] == "CLAIM_CONFLICT"
    assert client.post(f"/api/jobs/{job_id}/submission/release", json={"claim_token": token}).status_code == 200
    token = client.post(f"/api/jobs/{job_id}/submission/start").json()["claim"]["claim_token"]
    done = client.post(f"/api/jobs/{job_id}/applied", json={"claim_token": token, "confirmation_id": "GH-9"})
    assert done.status_code == 200
    with session(user) as db:
        application = db.scalar(select(Application).where(Application.job_id == job_id))
        assert application.status == "applied" and application.submitted_via == "apply_kit"
    state = client.get(f"/api/jobs/{job_id}/submission").json()
    assert state["claim"] is None and "outside Job Hunt OS" in state["warning"]


def test_a_manual_report_without_a_claim_is_still_recorded_and_checked(user, monkeypatch):
    client = client_for(monkeypatch, user)
    first = approved_job(user)
    duplicate = approved_job(user, url=URL + "?utm_source=mail")
    assert client.post(f"/api/jobs/{first}/applied", json={}).status_code == 200
    refused = client.post(f"/api/jobs/{duplicate}/applied", json={})
    assert refused.status_code == 409 and refused.json()["detail"]["status"] == "DUPLICATE_APPLICATION"
    with session(user) as db:
        application = db.scalar(select(Application).where(Application.job_id == first))
        assert application.submitted_via == "manual"


# ───────────── PostgreSQL: two clients at the same moment ─────────────

def test_two_clients_starting_together_get_exactly_one_claim(pg_sessions):
    from tests import qualification_support as qs

    sessions = qs.app_sessions(pg_sessions.kw["bind"])
    user_id = make_user(sessions)
    first = approved_job(user_id, factory=sessions)
    duplicate = approved_job(user_id, factory=sessions, url=URL + "/?utm_source=x")
    barrier, results = threading.Barrier(4), []

    def start(job_id, client):
        barrier.wait()
        results.append(begin(user_id, job_id, client, factory=sessions)[0])

    threads = [threading.Thread(target=start, args=(job_id, client))
               for job_id, client in ((first, "mcp"), (first, "apply_kit"), (duplicate, "mcp"),
                                      (duplicate, "apply_kit"))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert sorted(results).count("CLAIMED") == 1 and len(results) == 4
    assert set(results) <= {"CLAIMED", "CLAIM_CONFLICT"}
    with sessions() as db:
        assert len(db.scalars(select(ApplicationClaim).where(ApplicationClaim.status == "CLAIMED")).all()) == 1
