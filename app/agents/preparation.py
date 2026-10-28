"""The resume preparation graph (Phase 4): one qualified job from its resume to a person's approval, through the
existing services, with a bounded revision loop.

    SELECT_RESUME → LOAD_FIT_ANALYSIS → RETRIEVE_VERIFIED_EVIDENCE → BUILD_TAILOR_CONTEXT → TAILOR → VALIDATE_FACTS
      → REVIEW → DECIDE_PASS_OR_REVISION ─┬→ AWAIT_APPROVAL (a person decides)
                                          └→ TAILOR (revision + 1, at most `max_revisions` times)

A pass that saved its version but whose review failed is not tailored again: REVIEW queues one review-only pass of
that version (`tailor_tasks.enqueue(mode="review")`) and waits for it.

The graph is a conductor, not a second implementation. `resume_verification.for_job` selects the resume,
`context_builder.completed_fit_analysis` reads the Ranker's analysis (the graph never ranks: without one, or with one
made from another resume, the run ends), `rag_retrieval.scope` gives the job's evidence scope,
`context_builder.build_tailoring_context` is built read-only to refuse an incomplete context before anything is spent,
and `tailor_tasks.enqueue` queues the existing tailor worker. That worker makes the model calls, drops unsupported
units (`tailoring_results.apply_tailoring_patch`), saves a new version (`workflow.persist_tailored_resume`, which never
writes the source resume) and runs the Reviewer, under its own task row and lease. The graph then re-checks the saved
version deterministically (`tailoring_results.validate_tailored_resume`, lineage), and `review_results` decides
(`decide_review` / `apply_review`). A person approves, rejects or sends back in the existing screens
(`workflow.approve_job` and friends); the graph only waits for that and records it.

The graph itself never calls a model, a browser or the network.

Rules a node keeps:
  * A node writes only while its run holds the thread: `effects.within` locks the thread row, records the effect and
    makes the business write in one transaction. A replayed node finds the record and never queues a second tailoring
    pass; a stopped run raises LeaseLost and writes nothing.
  * Waiting is an interrupt. TAILOR waits for its tailor task (the claim skips the job while it is active); AWAIT_APPROVAL
    waits for a person (the claim skips the task until one decides).
  * A deterministic evidence problem always beats the reviewer: a version with an unsupported unit is never passed.
    At the revision limit the packet goes to a person (NEEDS_HUMAN), never through.
  * State is ids, enums, counts and hashes only (checked by GuardedSerializer); never resume text, a review or a prompt.

SHADOW runs walk the deterministic nodes read-only over a job the current path tailored and reviewed, and predict the
review decision (`decide_review` over the stored version) against what happened to the job. A shadow session is
rolled back and refuses to commit.
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import interrupt
from pydantic import BaseModel, ConfigDict
from sqlalchemy import desc, event, select
from sqlalchemy.orm import Session

from app.agents import effects

GRAPH = "preparation"
GRAPH_VERSION = "v1"
EXPORT_FIELDS = (
    "job_id", "task_id", "mode", "step", "outcome", "code", "waiting_for", "resume_id", "resume_version",
    "resume_hash", "ranking_id", "ranking_version", "evidence_count", "context_builder_version", "revision",
    "max_revisions", "tailor_task_id", "tailor_status", "variant_id", "variant_version", "lineage_ok", "fact_issues",
    "review_score", "recommendation", "decision", "disagreement", "approved_packet_id", "predicted_decision",
    "actual_decision",
)
ACTIVE_TAILOR = ("queued", "running")
ACTOR = "preparation"

# Node ids (stable: traces, Running workers and the task's `step` use them).
SELECT_RESUME = "SELECT_RESUME"
LOAD_FIT_ANALYSIS = "LOAD_FIT_ANALYSIS"
RETRIEVE = "RETRIEVE_VERIFIED_EVIDENCE"
BUILD_CONTEXT = "BUILD_TAILOR_CONTEXT"
TAILOR = "TAILOR"
VALIDATE_FACTS = "VALIDATE_FACTS"
REVIEW = "REVIEW"
DECIDE = "DECIDE_PASS_OR_REVISION"
AWAIT_APPROVAL = "AWAIT_APPROVAL"
NODES = (SELECT_RESUME, LOAD_FIT_ANALYSIS, RETRIEVE, BUILD_CONTEXT, TAILOR, VALIDATE_FACTS, REVIEW, DECIDE,
         AWAIT_APPROVAL)

# How a run ends (JobPreparationTask.outcome).
APPROVED, REJECTED, NEEDS_HUMAN, REMOVED, TRIAGED = "APPROVED", "REJECTED", "NEEDS_HUMAN", "REMOVED", "TRIAGED"
STOPPED, FAILED, SKIPPED, BLOCKED = "STOPPED", "FAILED", "SKIPPED", "BLOCKED"
PASS, RETAILOR = "PASS", "RETAILOR"
# What a person decided while the run waited at AWAIT_APPROVAL (JobPreparationTask.person_decision).
PERSON_DECISIONS = ("approved", "rejected", "revise")
# Deterministic issue codes that mean a claim the evidence does not support (a revision, never a pass).
FACT_CODES = frozenset({"UNKNOWN_EVIDENCE", "EVIDENCE_ROLE_MISMATCH", "UNCITED_CLAIM", "INVENTED_METRIC",
                        "UNSUPPORTED_TERM", "INVENTED_SKILL", "UNKNOWN_ROLE", "WORK_AUTHORIZATION",
                        "EVIDENCE_NOT_IN_CONTEXT"})


class PreparationInput(BaseModel):
    """What the worker starts a run with. Built on the server from the task row; never taken from a caller."""

    model_config = ConfigDict(extra="forbid")
    job_id: int
    task_id: int
    mode: Literal["ON", "SHADOW"] = "ON"
    reason: str = "new"
    max_revisions: int = 2


class PreparationResume(BaseModel):
    """The only value a resume carries: what the run waited for has happened (its node reads what from the rows)."""

    model_config = ConfigDict(extra="forbid")
    action: Literal["continue"]


class PreparationState(TypedDict, total=False):
    job_id: int
    task_id: int
    mode: str
    reason: str
    step: str
    outcome: str
    code: str
    waiting_for: str | None
    resume_at: str | None
    resume_id: int | None
    resume_version: int | None
    resume_hash: str | None
    ranking_id: int | None
    ranking_version: int | None
    evidence_count: int
    context_builder_version: str | None
    revision: int
    max_revisions: int
    tailor_task_id: int | None
    tailor_status: str | None
    variant_id: int | None
    variant_version: int | None
    previous_variant_id: int | None
    lineage_ok: bool
    fact_issues: int
    review_score: float | None
    recommendation: str | None
    decision: str | None
    disagreement: bool
    approved_packet_id: int | None
    predicted_decision: str | None
    actual_decision: str | None


@dataclass(frozen=True)
class PreparationContext:
    """Per-run context, never checkpointed: the lease, a session factory and test hooks."""

    run: effects.RunHandle
    sessions: Callable[[], Session]
    hold: Callable[[str], None] | None = None  # tests stop a process at named points
    task_id: int | None = None


class ShadowWrite(RuntimeError):
    """A shadow run tried to commit. Shadow runs only read."""


def _hold(ctx: PreparationContext, point: str) -> None:
    if ctx.hold is not None:
        ctx.hold(point)


@contextmanager
def _session(ctx: PreparationContext, read_only: bool) -> Iterator[Session]:
    """A session bound to the run's account. A read-only session refuses to commit and is always rolled back."""
    with ctx.sessions() as db:
        db.info["user_id"] = ctx.run.user_id
        if not read_only:
            yield db
            return

        def refuse(_session: Session) -> None:
            raise ShadowWrite("this step of the preparation graph never writes")

        event.listen(db, "before_commit", refuse)
        try:
            yield db
        finally:
            db.rollback()
            event.remove(db, "before_commit", refuse)


def _shadow(state: PreparationState) -> bool:
    return state.get("mode") == "SHADOW"


def _end(step: str, outcome: str, code: str | None = None, **values: Any) -> dict[str, Any]:
    return {"step": step, "outcome": outcome, "code": code or outcome, "waiting_for": None, "resume_at": None,
            **values}


def _wait(step: str, node: str, what: str, **values: Any) -> dict[str, Any]:
    return {"step": step, "waiting_for": what, "resume_at": node, **values}


def _hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


# ───────────── facts every node re-checks ─────────────

def _job(db: Session, state: PreparationState) -> Any:
    from app.models import Job

    return db.get(Job, state["job_id"])


def _terminal(db: Session, job: Any, step: str) -> dict[str, Any] | None:
    """The run ends here when the job is gone, a person has decided on it, or it left the preparation stages."""
    from app.services import approval

    if job is None:
        return _end(step, REMOVED, "JOB_DELETED")
    if job.status == "approved":
        packet = approval.live(db, job.application)
        return _end(step, APPROVED, "APPROVED", approved_packet_id=packet.id if packet else None)
    if job.status in ("dismissed", "rejected"):  # rejected from the queue, or the application set to rejected
        return _end(step, REJECTED, "REJECTED")
    if job.status == "applied":
        return _end(step, TRIAGED, "STATUS_APPLIED")
    if job.status == "archived":
        return _end(step, TRIAGED, "STATUS_ARCHIVED")
    if _desktop_mode(db, job.user_id):
        return _end(step, SKIPPED, "DESKTOP_MODE")
    return None


def _desktop_mode(db: Session, user_id: int) -> bool:
    """Claude Desktop does this account's tailoring and review (user_settings.execution_mode_for, read without
    creating the settings row: most steps run in a read-only session)."""
    from app.models import AppSetting

    row = db.scalar(select(AppSetting).where(AppSetting.user_id == user_id))
    return ((row.preferences if row is not None else None) or {}).get("execution_mode") == "claude_desktop"


def _profile(db: Session, job: Any) -> Any:
    from app.services import resume_verification as rv
    from app.services.workflow import get_profile

    return rv.for_job(db, get_profile(db), job)


def _variant(db: Session, variant_id: int | None) -> Any:
    from app.models import ResumeVariant

    return db.get(ResumeVariant, variant_id) if variant_id else None


def _baseline_variant(db: Session, job_id: int, task_id: int | None) -> Any:
    """The newest version saved before this task was queued: what a new pass must improve on. A retry (a new
    thread) uses the same baseline, so the pass an earlier attempt queued still counts as this task's."""
    from app.models import JobPreparationTask, ResumeVariant

    task = db.get(JobPreparationTask, task_id) if task_id else None
    query = select(ResumeVariant).where(ResumeVariant.job_id == job_id)
    if task is not None:
        query = query.where(ResumeVariant.created_at < task.created_at)
    return db.scalar(query.order_by(desc(ResumeVariant.version)).limit(1))


def _latest_variant(db: Session, job_id: int) -> Any:
    from app.models import ResumeVariant

    return db.scalar(select(ResumeVariant).where(ResumeVariant.job_id == job_id)
                     .order_by(desc(ResumeVariant.version)).limit(1))


# ───────────── SELECT_RESUME ─────────────

def select_resume(state: PreparationState, runtime: Runtime[PreparationContext]) -> dict[str, Any]:
    """The job's resume: its pin, else the active one (resume_verification.for_job). Never activates anything."""
    ctx = runtime.context
    from app.services import resume_verification as rv

    # Writable in ON mode, under the lease: the first use of a profile records its master text as a resume version
    # (resume_verification.active_resume), as the tailor path itself would. Nothing else is written.
    with _session(ctx, _shadow(state)) as db:
        job = _job(db, state)
        done = None if _shadow(state) and job is not None else _terminal(db, job, SELECT_RESUME)
        if done:
            return done
        if not _shadow(state):
            effects.held(db, ctx.run)
        profile = _profile(db, job)
        if not _shadow(state):
            db.commit()
        if profile is None:
            return _end(SELECT_RESUME, BLOCKED, "NO_PROFILE")
        resume = rv.resume_for(db, profile, create=False)
        previous = _baseline_variant(db, job.id, state.get("task_id"))
        return {"step": SELECT_RESUME, "resume_id": getattr(resume, "id", None),
                "resume_version": getattr(resume, "version", None), "resume_hash": _hash(rv.raw_text(profile)),
                "revision": state.get("revision", 0), "max_revisions": state.get("max_revisions", 2),
                "previous_variant_id": previous.id if previous is not None else None, "fact_issues": 0,
                "disagreement": False}


# ───────────── LOAD_FIT_ANALYSIS ─────────────

def load_fit_analysis(state: PreparationState, runtime: Runtime[PreparationContext]) -> dict[str, Any]:
    """The Ranker's completed analysis, read as it is. Missing, or made from another resume: the run ends and the job
    stays where it is (the ranker's own sweep re-ranks it). The graph never queues a ranking."""
    ctx = runtime.context
    from app.models import JobRanking
    from app.services import ranker
    from app.services import resume_verification as rv
    from app.services.context_builder import ContextError, completed_fit_analysis

    with _session(ctx, True) as db:
        job = _job(db, state)
        if job is None:
            return _end(LOAD_FIT_ANALYSIS, REMOVED, "JOB_DELETED")
        try:
            analysis = completed_fit_analysis(db, job)
        except ContextError as exc:
            return _end(LOAD_FIT_ANALYSIS, BLOCKED, exc.code)
        ranking = db.get(JobRanking, analysis.get("ranking_id")) if analysis.get("ranking_id") else None
        if ranking is not None and not _shadow(state):
            profile = _profile(db, job)
            if profile is not None and ranking.resume_content_hash != ranker.content_hash(rv.raw_text(profile)):
                return _end(LOAD_FIT_ANALYSIS, BLOCKED, "STALE_RANKING", ranking_id=ranking.id)
        return {"step": LOAD_FIT_ANALYSIS, "ranking_id": analysis.get("ranking_id"),
                "ranking_version": analysis.get("ranking_version")}


# ───────────── RETRIEVE_VERIFIED_EVIDENCE ─────────────

def retrieve(state: PreparationState, runtime: Runtime[PreparationContext]) -> dict[str, Any]:
    """The verified evidence the job's resume may draw on (rag_retrieval.scope). Ids only: the tailor builds its own
    context from the same scope, so the graph never sends a second copy."""
    ctx = runtime.context
    from app.services import rag_retrieval

    with _session(ctx, True) as db:
        job = _job(db, state)
        if job is None:
            return _end(RETRIEVE, REMOVED, "JOB_DELETED")
        profile = _profile(db, job)
        try:
            _resume, ids = rag_retrieval.scope(db, profile, job) if profile is not None else (None, set())
        except Exception:  # noqa: BLE001  the context build reports a missing resume itself
            ids = set()
        return {"step": RETRIEVE, "evidence_count": len(ids)}


# ───────────── BUILD_TAILOR_CONTEXT ─────────────

def build_context(state: PreparationState, runtime: Runtime[PreparationContext]) -> dict[str, Any]:
    """Build the Tailor's context read-only, to refuse an incomplete one (no verified facts, no analysis) before a
    tailoring pass is queued. Nothing is kept but its version and size: the worker builds it again when it runs."""
    ctx = runtime.context
    from app.services.context_builder import (
        CONTEXT_BUILDER_VERSION,
        ContextError,
        build_tailoring_context,
    )

    with _session(ctx, True) as db:
        job = _job(db, state)
        if job is None:
            return _end(BUILD_CONTEXT, REMOVED, "JOB_DELETED")
        try:
            context = build_tailoring_context(db, _profile(db, job), job)
        except ContextError as exc:
            return _end(BUILD_CONTEXT, BLOCKED, exc.code)
        except ValueError as exc:  # UnverifiedResumeError and the like: nothing verified to tailor from
            return _end(BUILD_CONTEXT, BLOCKED, type(exc).__name__.upper()[:60])
        return {"step": BUILD_CONTEXT, "context_builder_version": CONTEXT_BUILDER_VERSION,
                "evidence_count": max(state.get("evidence_count", 0), len(context.evidence_ids))}


# ───────────── TAILOR ─────────────

def tailor(state: PreparationState, runtime: Runtime[PreparationContext]) -> dict[str, Any]:
    """Queue one tailoring pass for this revision (once, whatever replays) and wait for the tailor worker."""
    ctx = runtime.context
    from app.models import TailorTask
    from app.services import tailor_tasks

    if _shadow(state):
        with _session(ctx, True) as db:
            latest = _latest_variant(db, state["job_id"])
            found = (latest.id, latest.version) if latest is not None else (None, None)
        return {"step": TAILOR, "variant_id": found[0], "variant_version": found[1]}
    revision = state.get("revision", 0)
    with _session(ctx, False) as db:
        job = _job(db, state)
        done = _terminal(db, job, TAILOR)
        if done:
            return done
        record, created = effects.within(db, ctx.run, f"tailor.enqueue:{job.id}:r{revision}")
        if created:
            options: dict[str, Any] = {"conducted": True}
            if revision and state.get("variant_id"):
                options["revise_from"] = state["variant_id"]
            queued = tailor_tasks.enqueue(db, [job.id], regenerate=revision > 0, options=options, commit=False)
            task = (queued["queued"] or [None])[0]
            reason = (queued["skipped"] or [{}])[0].get("reason")
            record.summary = {"task": task["id"] if task else None, "skipped": (reason or "")[:80] or None}
        db.commit()
        summary = record.summary or {}
        task = db.get(TailorTask, summary["task"]) if summary.get("task") else None
        if task is None:
            task = _adopt(db, job.id, state)
        if task is None:
            return _end(TAILOR, SKIPPED, "TAILOR_NOT_QUEUED")
        task_id, status = task.id, task.status
    _hold(ctx, "tailor:after_enqueue")
    if status in ACTIVE_TAILOR:
        return _wait(TAILOR, "tailor", "TAILORING", tailor_task_id=task_id, tailor_status=status)
    return {"step": TAILOR, "tailor_task_id": task_id, "tailor_status": status, "waiting_for": None,
            "resume_at": None}


def _adopt(db: Session, job_id: int, state: PreparationState) -> Any:
    """The pass to wait for when this run's own enqueue queued nothing: one already queued (a person's Tailor button),
    or the pass an earlier attempt of this same task queued before its worker died (a retry is a new thread, so it has
    no record of it). Never a pass from before this task existed."""
    from app.models import JobPreparationTask, TailorTask

    active = db.scalar(select(TailorTask).where(TailorTask.job_id == job_id, TailorTask.status.in_(ACTIVE_TAILOR))
                       .limit(1))
    if active is not None:
        return active
    task = db.get(JobPreparationTask, state.get("task_id")) if state.get("task_id") else None
    if task is None:
        return None
    previous = _variant(db, state.get("previous_variant_id"))
    for row in db.scalars(select(TailorTask).where(TailorTask.job_id == job_id, TailorTask.created_at >= task.created_at)
                          .order_by(desc(TailorTask.id))).all():
        made = _variant(db, (row.result or {}).get("resume_id"))
        if made is not None and (previous is None or made.version > previous.version):
            return row
    return None


def _after_tailor_task(state: PreparationState, runtime: Runtime[PreparationContext]) -> dict[str, Any]:
    """The pass ended: find the version it saved (newer than the one before it), or end the run."""
    ctx = runtime.context
    from app.models import TailorTask
    from app.services.lifecycle import STOPPED_BY_USER

    if _shadow(state):
        return {}
    with _session(ctx, True) as db:
        task = db.get(TailorTask, state.get("tailor_task_id")) if state.get("tailor_task_id") else None
        if task is None:
            return _end(TAILOR, FAILED, "TAILOR_TASK_MISSING")
        if task.status == "cancelled":
            stopped = task.error in (None, STOPPED_BY_USER) or "stopped" in (task.error or "").lower()
            return _end(TAILOR, STOPPED if stopped else SKIPPED, "TAILOR_CANCELLED")
        previous = _variant(db, state.get("previous_variant_id"))
        if task.status == "failed":
            # The pass saved its version and only the review failed: keep the version, REVIEW asks for the review.
            saved = _latest_variant(db, state["job_id"]) if task.stage == "reviewing" else None
            if saved is None or saved.reviewer_score is not None or saved.created_at < task.created_at or (
                    previous is not None and saved.version <= previous.version):
                return _end(TAILOR, FAILED, "TAILOR_FAILED")
            return {"variant_id": saved.id, "variant_version": saved.version}
        variant_id = (task.result or {}).get("resume_id")
        variant = _variant(db, variant_id)
        if variant is None or variant.job_id != state["job_id"] or (
                previous is not None and variant.version <= previous.version):
            return _end(TAILOR, FAILED, "NO_NEW_VERSION")
        return {"variant_id": variant.id, "variant_version": variant.version}


# ───────────── VALIDATE_FACTS ─────────────

def validate_facts(state: PreparationState, runtime: Runtime[PreparationContext]) -> dict[str, Any]:
    """Re-check the saved version deterministically: its lineage (the selected resume, the loaded ranking) and every
    unit against the job's verified, in-scope evidence. Read-only; a problem is counted, never repaired here."""
    ctx = runtime.context
    from app.services.tailoring_results import from_variant_content, validate_tailored_resume

    found = _after_tailor_task(state, runtime)
    if found.get("outcome"):
        return found
    state = {**state, **found}  # type: ignore[typeddict-item]
    with _session(ctx, True) as db:
        job = _job(db, state)
        variant = _variant(db, state.get("variant_id"))
        if job is None or variant is None:
            return _end(VALIDATE_FACTS, REMOVED if job is None else FAILED, "VERSION_MISSING")
        content = variant.content or {}
        lineage_ok = lineage_matches(content, state.get("resume_id"), state.get("ranking_id"))
        issues: list[dict[str, Any]] = []
        if not content.get("untailored"):
            data, legacy = from_variant_content(job.id, content)
            issues = validate_tailored_resume(db, _profile(db, job), job, data, legacy=legacy)
        fact_issues = sum(1 for i in issues if i.get("code") in FACT_CODES)
        return {**found, "step": VALIDATE_FACTS, "lineage_ok": lineage_ok, "fact_issues": fact_issues}


# ───────────── REVIEW ─────────────

def review(state: PreparationState, runtime: Runtime[PreparationContext]) -> dict[str, Any]:
    """The Reviewer's stored review of this version (the tailor worker ran it). A version saved without one (its
    review failed) gets one review-only pass, and the run waits for it; a packet is never decided without a review."""
    ctx = runtime.context
    with _session(ctx, True) as db:
        variant = _variant(db, state.get("variant_id"))
        if variant is None:
            return _end(REVIEW, FAILED, "VERSION_MISSING")
        if variant.reviewer_score is not None:
            notes = variant.reviewer_notes or {}
            return {"step": REVIEW, "review_score": float(variant.reviewer_score),
                    "recommendation": str(notes.get("recommendation") or "")[:12] or None, "waiting_for": None,
                    "resume_at": None}
        version = variant.version
    if _shadow(state):
        return _end(REVIEW, FAILED, "REVIEW_MISSING")
    return _review_pass(state, runtime, version)


def _review_pass(state: PreparationState, runtime: Runtime[PreparationContext], version: int) -> dict[str, Any]:
    """Queue one review-only pass of this version (once, whatever replays) and wait for it. Back here after it ended
    with the version still unreviewed, the run ends: the pass is never queued twice."""
    ctx = runtime.context
    from app.models import TailorTask
    from app.services import tailor_tasks
    from app.services.lifecycle import STOPPED_BY_USER

    with _session(ctx, False) as db:
        job = _job(db, state)
        done = _terminal(db, job, REVIEW)
        if done:
            return done
        record, created = effects.within(db, ctx.run, f"review.enqueue:{job.id}:v{version}")
        if created:
            queued = tailor_tasks.enqueue(db, [job.id], mode="review", options={"conducted": True}, commit=False)
            task = (queued["queued"] or [None])[0]
            reason = (queued["skipped"] or [{}])[0].get("reason")
            record.summary = {"task": task["id"] if task else None, "skipped": (reason or "")[:80] or None}
        db.commit()
        summary = record.summary or {}
        task = db.get(TailorTask, summary["task"]) if summary.get("task") else None
        if task is None:  # a pass already queued (a person's Send to review) is the one to wait for
            task = db.scalar(select(TailorTask).where(TailorTask.job_id == job.id,
                                                      TailorTask.status.in_(ACTIVE_TAILOR)).limit(1))
        if task is None:
            return _end(REVIEW, FAILED, "REVIEW_MISSING")
        status, error = task.status, task.error
    _hold(ctx, "review:after_enqueue")
    if status in ACTIVE_TAILOR:
        return _wait(REVIEW, "review", "REVIEWING")
    if status == "cancelled":
        stopped = error in (None, STOPPED_BY_USER) or "stopped" in (error or "").lower()
        return _end(REVIEW, STOPPED if stopped else SKIPPED, "REVIEW_CANCELLED")
    return _end(REVIEW, FAILED, "REVIEW_FAILED" if status == "failed" else "REVIEW_MISSING")


# ───────────── DECIDE_PASS_OR_REVISION ─────────────

def _predict(db: Session, job: Any, variant: Any, state: PreparationState) -> tuple[str, bool]:
    """review_results.decide_review over the stored version, with the graph's own checks on top: a fact problem or a
    broken lineage is a revision whatever the reviewer said (`disagreement` when it said PASS)."""
    from app.services.review_results import decide_review, review_of

    found = review_of(job.id, variant)
    if found is None:
        return "NEEDS_HUMAN", False
    return overrule(decide_review(db, job, variant, found).decision, found.recommendation,
                    state.get("fact_issues", 0), state.get("lineage_ok", True))


def overrule(decision: str, recommendation: str, fact_issues: int, lineage_ok: bool) -> tuple[str, bool]:
    """The graph's checks on top of review_results' decision: an unsupported claim or a version that is not of the
    selected resume and ranking is a revision whatever the reviewer said. Returns (decision, disagreement), where
    disagreement means the reviewer recommended PASS and the checks did not. Pure."""
    forced = fact_issues > 0 or not lineage_ok
    return (RETAILOR if forced else decision), bool(forced and recommendation == "PASS")


def lineage_matches(content: dict[str, Any], resume_id: int | None, ranking_id: int | None) -> bool:
    """A saved version was tailored from the selected source resume and the loaded ranking (as its stored context
    says). An untailored packet is the source resume itself. Unknown on either side is not a mismatch. Pure."""
    if content.get("untailored"):
        return True
    context = content.get("context") or {}
    fit = context.get("fit_analysis") or {}
    return ((resume_id is None or context.get("id") in (None, resume_id))
            and (ranking_id is None or fit.get("ranking_id") in (None, ranking_id)))


def decide(state: PreparationState, runtime: Runtime[PreparationContext]) -> dict[str, Any]:
    """Python decides (review_results); the job's status moves through `apply_review`, once per version. A revision
    goes back to TAILOR until `max_revisions`; past it the packet waits for a person (NEEDS_HUMAN), never passes."""
    ctx = runtime.context
    from app.services.review_results import ReviewIn, apply_review, review_of

    with _session(ctx, _shadow(state)) as db:
        job = _job(db, state)
        variant = _variant(db, state.get("variant_id"))
        if _shadow(state):
            if job is None or variant is None:
                return _end(DECIDE, REMOVED, "JOB_DELETED")
            predicted, disagreement = _predict(db, job, variant, state)
            return _end(DECIDE, predicted, "SHADOW", predicted_decision=predicted, disagreement=disagreement,
                        actual_decision=_actual(job))
        done = _terminal(db, job, DECIDE)
        if done:
            return done
        if variant is None:
            return _end(DECIDE, FAILED, "VERSION_MISSING")
        decision, disagreement = _predict(db, job, variant, state)
        revision, limit = state.get("revision", 0), state.get("max_revisions", 2)
        route, at_limit = next_step(decision, revision, limit)
        record, created = effects.within(db, ctx.run, f"decide:{job.id}:v{variant.version}")
        if created:
            found = review_of(job.id, variant)
            if found is not None and decision == RETAILOR and found.recommendation == "PASS":
                # The evidence checks overrule the reviewer: the stored review records why.
                found = ReviewIn(**{**found.model_dump(), "recommendation": "RETAILOR"})
            if found is not None:
                made = apply_review(db, job, variant, found, source="preparation_graph", commit=False)
                if decision == RETAILOR and made.decision != RETAILOR:
                    job.status = "needs_retailor"
            if at_limit:
                job.status = "pending_approval"  # a person decides; the packet never passes by running out of tries
            record.summary = {"decision": decision, "at_limit": at_limit}
        db.commit()
        _maybe_auto_approve(db, ctx.run, job, variant, decision)
    if route == "tailor":
        return {"step": DECIDE, "decision": decision, "disagreement": disagreement, "revision": revision + 1,
                "previous_variant_id": state.get("variant_id")}
    return {"step": DECIDE, "decision": NEEDS_HUMAN if at_limit else decision, "disagreement": disagreement,
            "code": "REVISION_LIMIT" if at_limit else decision}


def next_step(decision: str, revision: int, limit: int) -> tuple[str, bool]:
    """Where a review decision sends the packet: ("tailor", False) for another revision while `revision` < `limit`,
    else ("await_approval", at_limit). A RETAILOR at the limit waits for a person (at_limit True); it never passes.
    Pure: the bounded loop's whole rule, used by DECIDE and by the evaluations."""
    if decision == RETAILOR:
        return ("tailor", False) if revision < max(0, limit) else ("await_approval", True)
    return "await_approval", False


def _maybe_auto_approve(db: Session, run: effects.RunHandle, job: Any, variant: Any, decision: str) -> None:
    """An account in automatic application mode approves a PASS at once, as the tailor worker did before the graph.
    Only a PASS, and only while the packet still waits as that PASS (status `tailored`, this version the newest):
    a replay after a crash between the decision and the approval approves it then, and an approved, rejected or
    sent-back packet is never approved here. A refused approval (approval.ApprovalRefused) leaves it to a person."""
    from app.services.approval import ApprovalRefused
    from app.services.user_settings import settings_for
    from app.services.workflow import approve_job

    if decision != PASS or job.status != "tailored" or _latest_variant(db, job.id) is not variant:
        return
    setting = settings_for(db, job.user_id)
    if setting.application_mode != "automatic" or (variant.reviewer_score or 0) < setting.min_review_score:
        return
    try:
        effects.held(db, run)  # only while this run holds its thread: a stopped run approves nothing
        approve_job(db, job)  # commits with the lease row still locked
    except (ApprovalRefused, ValueError):
        db.rollback()


def _actual(job: Any) -> str:
    """What the current path's packet decision was, from the job's status."""
    return {"tailored": PASS, "needs_retailor": RETAILOR, "pending_approval": "NEEDS_HUMAN",
            "approved": PASS, "applied": PASS}.get(job.status, "UNKNOWN")


def _after_decide(state: PreparationState) -> str:
    if state.get("outcome"):
        return END
    return "tailor" if state.get("decision") == RETAILOR else "await_approval"


# ───────────── AWAIT_APPROVAL ─────────────

def await_approval(state: PreparationState, runtime: Runtime[PreparationContext]) -> dict[str, Any]:
    """Wait for a person. Approval, rejection and send-back happen in the existing screens; this node reads what they
    did. Sent back: another revision while the bound allows, else the run ends with the packet in the person's hands."""
    ctx = runtime.context
    from app.models import JobPreparationTask

    with _session(ctx, False) as db:
        job = _job(db, state)
        done = _terminal(db, job, AWAIT_APPROVAL)
        if done:
            return done
        task = db.get(JobPreparationTask, state.get("task_id")) if state.get("task_id") else None
        asked = task.person_decision if task is not None else None
        if asked == "revise":
            revision, limit = state.get("revision", 0), state.get("max_revisions", 2)
            record, created = effects.within(db, ctx.run, f"revise:{job.id}:r{revision}")
            if created:
                task.person_decision = None
                record.summary = {"revision": revision}
            db.commit()
            if revision >= limit:
                return _end(AWAIT_APPROVAL, NEEDS_HUMAN, "REVISION_LIMIT")
            return {"step": AWAIT_APPROVAL, "decision": RETAILOR, "revision": revision + 1,
                    "previous_variant_id": state.get("variant_id"), "waiting_for": None, "resume_at": None}
    return _wait(AWAIT_APPROVAL, "await_approval", "APPROVAL", decision=state.get("decision"))


def _after_await(state: PreparationState) -> str:
    if state.get("outcome"):
        return END
    if state.get("waiting_for"):
        return "wait"
    return "tailor"


# ───────────── waiting ─────────────

def wait(state: PreparationState) -> dict[str, Any]:
    """Settle the run until the work or the person it waits for is done; the worker resumes it then."""
    answer = interrupt({"wait": state.get("waiting_for"), "step": state.get("step"), "job_id": state.get("job_id")})
    PreparationResume.model_validate(answer if isinstance(answer, dict) else {"action": answer})
    return {"waiting_for": None}


def _next(after: str) -> Callable[[PreparationState], str]:
    def route(state: PreparationState) -> str:
        if state.get("outcome"):
            return END
        if state.get("waiting_for"):
            return "wait"
        return after
    return route


def _after_wait(state: PreparationState) -> str:
    return state.get("resume_at") or "select_resume"


def build(checkpointer: Any) -> Any:
    graph = StateGraph(PreparationState, context_schema=PreparationContext)
    graph.add_node("select_resume", select_resume)
    graph.add_node("load_fit_analysis", load_fit_analysis)
    graph.add_node("retrieve", retrieve)
    graph.add_node("build_context", build_context)
    graph.add_node("tailor", tailor)
    graph.add_node("validate_facts", validate_facts)
    graph.add_node("review", review)
    graph.add_node("decide", decide)
    graph.add_node("await_approval", await_approval)
    graph.add_node("wait", wait)
    graph.add_edge(START, "select_resume")
    graph.add_conditional_edges("select_resume", _next("load_fit_analysis"), ["load_fit_analysis", END])
    graph.add_conditional_edges("load_fit_analysis", _next("retrieve"), ["retrieve", END])
    graph.add_conditional_edges("retrieve", _next("build_context"), ["build_context", END])
    graph.add_conditional_edges("build_context", _next("tailor"), ["tailor", END])
    graph.add_conditional_edges("tailor", _next("validate_facts"), ["validate_facts", "wait", END])
    graph.add_conditional_edges("validate_facts", _next("review"), ["review", END])
    graph.add_conditional_edges("review", _next("decide"), ["decide", "wait", END])
    graph.add_conditional_edges("decide", _after_decide, ["tailor", "await_approval", END])
    graph.add_conditional_edges("await_approval", _after_await, ["tailor", "wait", END])
    graph.add_conditional_edges("wait", _after_wait, ["tailor", "review", "await_approval"])
    return graph.compile(checkpointer=checkpointer, name=GRAPH)


def summary(state: dict[str, Any]) -> dict[str, Any]:
    """The finished run's result as stored on the thread: safe fields only."""
    return {k: state[k] for k in EXPORT_FIELDS if k in state and state[k] is not None}
