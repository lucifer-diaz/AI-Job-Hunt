"""The approved packet: what a person approved, kept exactly, and the job's canonical state at approval (Phase 4).

    workflow.approve_job → reconcile_to_ready (validated transitions to READY_TO_APPLY, or ApprovalRefused)
                         → snapshot (an immutable approved_packets row: content + digest)
    an edit of an approved packet → revoke (same transaction as the edit: approval and digest withdrawn,
                                    READY_TO_APPLY → REVIEWED, back in the approval queue)
    export, packet issuance, application → verify (the packet about to be used against the approved digest)

Rules:
  * The digest is computed once, at approval, from a documented canonical form (`canonical`, SERIALIZER_VERSION). A
    mismatch later means the packet changed: it needs a new approval. Nothing ever re-hashes a packet to make it
    "approved" again.
  * The job's canonical state moves only through `work_queue.transition`, and only through stages a stored fact
    backs: a saved tailored version is TAILORING → TAILORED, a stored review of it is REVIEWED. A job whose state has
    no such path (a re-rank in progress, archived, skipped, failed) is refused, and nothing is written.
  * A revoked approval keeps its row (`revoked_at`, `revoked_reason`): the history of what was approved stays.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.models import Application, ApprovedPacket, Job, ResumeVariant, WorkflowTransition
from app.services.lifecycle import WorkflowState as S
from app.services.lifecycle import can_transition, parse_state

SERIALIZER_VERSION = 1
ACTOR = "approval"
RECONCILE_ACTOR = "reconcile"
# Where an approval may start from: the stages before READY_TO_APPLY that tailoring and review stand for, and
# READY_TO_APPLY itself (approving again).
PREPARATION_STATES = (S.QUALIFIED, S.TAILORING, S.TAILORED, S.REVIEWED, S.READY_TO_APPLY)
SUBMITTED_STATUSES = ("applied",)


class ApprovalRefused(ValueError):
    """The job's canonical state cannot reach READY_TO_APPLY through stages its stored facts back."""


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _text(value: str | None) -> str:
    return (value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def packet_content(variant: ResumeVariant) -> dict[str, Any]:
    """What the recruiter receives from this version: the rendered resume, the letter and the attachment identity."""
    from app.services.resume_format import strip_citations

    return {"resume_markdown": _text(variant.markdown), "cover_letter": _text(strip_citations(variant.cover_letter)),
            "attachments": [{"kind": "resume", "variant_id": variant.id, "variant_version": variant.version}]}


def canonical(content: dict[str, Any]) -> bytes:
    """Serializer v1: UTF-8 JSON, keys sorted, no insignificant whitespace, newlines normalised to \\n, each text
    stripped. The serializer version is part of the hashed bytes, so a future format can never collide with v1."""
    return json.dumps({"serializer_version": SERIALIZER_VERSION, **content}, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def digest_of(content: dict[str, Any]) -> str:
    return hashlib.sha256(canonical(content)).hexdigest()


# ───────────── canonical state ─────────────

def _last_entered(db: Session, job: Job, state: S) -> datetime | None:
    return db.scalar(select(WorkflowTransition.created_at).where(WorkflowTransition.job_id == job.id,
                                                                 WorkflowTransition.to_state == state)
                     .order_by(desc(WorkflowTransition.id)).limit(1))


def path_to_ready(db: Session, job: Job, variant: ResumeVariant) -> list[tuple[S, str]] | None:
    """The transitions that take the job to READY_TO_APPLY, each with the stored fact it stands for; [] when it is
    already there; None when no such path exists."""
    state = parse_state(job.state)
    tailored = f"tailored version {variant.version} (resume_variants.id {variant.id}) saved " \
               f"{variant.created_at:%Y-%m-%d %H:%M}" if variant.created_at else f"tailored version {variant.version}"
    reviewed = f"review of version {variant.version} stored (score {variant.reviewer_score:g})"
    stages = [(S.TAILORING, tailored), (S.TAILORED, tailored), (S.REVIEWED, reviewed)]
    if state is S.READY_TO_APPLY:
        return []
    if state is S.REVIEWED:
        # A version saved after the job last entered REVIEWED is a new tailoring pass: record it, then approve.
        entered = _last_entered(db, job, S.REVIEWED)
        newer = entered is not None and variant.created_at is not None and variant.created_at > entered
        steps = stages if newer else []
    elif state is S.QUALIFIED:
        steps = stages
    elif state is S.TAILORING:
        steps = stages[1:]
    elif state is S.TAILORED:
        steps = stages[2:]
    else:
        return None
    steps = [*steps, (S.READY_TO_APPLY, f"approved: version {variant.version}")]
    current, previous = state, parse_state(job.previous_state) if job.previous_state else None
    for target, _ in steps:
        if not can_transition(current, target, previous_state=previous):
            return None
        current, previous = target, current
    return steps


def refusal_reason(job: Job) -> str:
    """Why approval is refused, and what puts the job back (the action ranker.pipeline_status offers on its screens)."""
    from app.services.lifecycle import REOPENABLE_ARCHIVE_SOURCES

    state = parse_state(job.state)
    previous = parse_state(job.previous_state) if job.previous_state else None
    if state in (S.RANK_PENDING, S.RANKING, S.RANKED):
        return "This job is being ranked again; approve it once the new ranking is in"
    if state is S.FAILED and previous in (S.RANK_PENDING, S.RANKING):
        return ("This job's last ranking failed, so it cannot be approved yet. Use \"Put back in ranking\" on the "
                "job and approve it once it qualifies again")
    if state is S.ARCHIVED and previous in REOPENABLE_ARCHIVE_SOURCES:
        return ("The ranking policy archived this job, so it cannot be approved. Use \"Rank again\" on the job "
                "first")
    return (f"This job's workflow state is {state.value}, which cannot move to ready-to-apply. "
            "Re-rank or restore the job first")


def reconcile_to_ready(db: Session, job: Job, variant: ResumeVariant, *, actor: str = ACTOR,
                       now: datetime | None = None) -> list[WorkflowTransition]:
    """Move the job to READY_TO_APPLY through validated transitions, or raise ApprovalRefused. Does not commit.

    Stages the job's state has not caught up with are recorded by RECONCILE_ACTOR with the fact they stand for (the
    version and when it was saved); the last step is the approval itself."""
    from app.services.work_queue import transition

    steps = path_to_ready(db, job, variant)
    if steps is None:
        raise ApprovalRefused(refusal_reason(job))
    now = now or utcnow()
    made = []
    for target, fact in steps:
        who = actor if target is S.READY_TO_APPLY else RECONCILE_ACTOR
        why = fact if target is S.READY_TO_APPLY else f"Reconciled at approval: {fact}"
        made.append(transition(db, job, target, actor=who, reason=why, now=now))
    return made


# ───────────── the approved packet ─────────────

def live(db: Session, application: Application | None) -> ApprovedPacket | None:
    """The application's current approval snapshot (newest, not revoked)."""
    if application is None or application.id is None:
        return None
    # The job too: on SQLite a deleted application's id can come back, and its rows must never match.
    return db.scalar(select(ApprovedPacket).where(ApprovedPacket.application_id == application.id,
                                                  ApprovedPacket.job_id == application.job_id,
                                                  ApprovedPacket.revoked_at.is_(None))
                     .order_by(desc(ApprovedPacket.id)).limit(1))


def snapshot(db: Session, application: Application, variant: ResumeVariant, *, override: bool = False,
             now: datetime | None = None) -> ApprovedPacket:
    """Record what was approved. An earlier live snapshot is revoked as superseded. Does not commit."""
    now = now or utcnow()
    db.flush()  # the application needs its id
    previous = live(db, application)
    if previous is not None:
        previous.revoked_at, previous.revoked_reason = now, "superseded by a new approval"
    content = packet_content(variant)
    packet = ApprovedPacket(user_id=application.job.user_id if application.job else variant.user_id,
                            application_id=application.id, job_id=variant.job_id, variant_id=variant.id,
                            variant_version=variant.version, serializer_version=SERIALIZER_VERSION,
                            digest=digest_of(content), content=content, reviewer_score=variant.reviewer_score,
                            override=bool(override), created_at=now)
    db.add(packet)
    return packet


@dataclass(frozen=True)
class Verification:
    ok: bool
    reason: str | None = None
    packet: ApprovedPacket | None = None


def verify(db: Session, application: Application | None) -> Verification:
    """Is the packet about to be used the one a person approved? Compares the approved resume as it is now against
    the digest taken at approval. Approvals made before Phase 4 have no snapshot: they are reported as such (`ok`,
    reason LEGACY) and keep working as they always did."""
    if application is None or application.status not in ("approved", "applied"):
        return Verification(False, "not approved")
    packet = live(db, application)
    if packet is None:
        return Verification(True, "LEGACY")
    variant = db.get(ResumeVariant, application.approved_resume_id) if application.approved_resume_id else None
    if variant is None or variant.id != packet.variant_id:
        return Verification(False, "The approved resume is no longer the one that was approved; approve it again",
                            packet)
    if digest_of(packet_content(variant)) != packet.digest:
        return Verification(False, "The approved packet was changed after approval; approve it again", packet)
    return Verification(True, None, packet)


def revoke(db: Session, job: Job, reason: str, *, now: datetime | None = None) -> bool:
    """Withdraw an approval because its packet is being changed. Same transaction as the change; does not commit.
    True when an approval was revoked. A submitted application is never revoked (its packet must not change)."""
    from app.services.work_queue import transition

    application = job.application
    if application is None or application.status != "approved":
        return False
    now = now or utcnow()
    withdraw(db, application, reason, now=now)
    application.status = "pending_approval"
    application.approved_at = None
    job.status = "pending_approval"
    if parse_state(job.state) is S.READY_TO_APPLY:
        transition(db, job, S.REVIEWED, actor=ACTOR, reason=f"Approval revoked: {reason}"[:500], now=now)
    return True


def withdraw(db: Session, application: Application | None, reason: str, *, now: datetime | None = None) -> bool:
    """Mark the application's live approval snapshot revoked (the row stays as history). Does not commit. True when
    there was one. The caller moves the application and the job (revoke, or a rejection)."""
    packet = live(db, application)
    if packet is None:
        return False
    packet.revoked_at, packet.revoked_reason = now or utcnow(), reason[:200]
    return True


def guard_edit(db: Session, variant: ResumeVariant, what: str) -> None:
    """Called before an in-place edit of a tailored version. A submitted packet cannot change (ValueError); an
    approved one loses its approval in the same transaction as the edit."""
    job = db.get(Job, variant.job_id)
    application = job.application if job is not None else None
    if application is None:
        return
    if application.status in SUBMITTED_STATUSES or application.applied_at is not None:
        raise ValueError("This application has been submitted; its packet can no longer be edited")
    if application.status == "approved" and application.approved_resume_id == variant.id:
        revoke(db, job, f"{what} edited after approval")


def export_variant(db: Session, job_id: int) -> ResumeVariant | None:
    """The version a download renders: the approved one while the job's application is approved or submitted (a
    newer version saved later is not what was approved), else the newest."""
    job = db.get(Job, job_id)
    application = job.application if job is not None else None
    if application is not None and application.status in ("approved", "applied") and application.approved_resume_id:
        approved = db.get(ResumeVariant, application.approved_resume_id)
        if approved is not None and approved.job_id == job_id:
            return approved
    return db.scalar(select(ResumeVariant).where(ResumeVariant.job_id == job_id)
                     .order_by(desc(ResumeVariant.version)).limit(1))


def check_export(db: Session, job_id: int) -> None:
    """Before a download of an approved (or submitted) packet: refuse (PacketChanged) when it no longer matches what
    was approved. Packets not approved yet download as they are."""
    job = db.get(Job, job_id)
    application = job.application if job is not None else None
    if application is None or application.status not in ("approved", "applied"):
        return
    result = verify(db, application)
    if not result.ok:
        raise PacketChanged(result.reason or "The approved packet changed")


class PacketChanged(ValueError):
    """The packet about to be used differs from the one a person approved."""
