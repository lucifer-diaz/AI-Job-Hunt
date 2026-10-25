"""Guarded submission (Phase 6): the only path by which Job Hunt OS itself may submit an application.

A GUARDED_AUTO execution reaches the final click only through these steps, in this order, and no database
transaction is held while the browser works:

    1. PreSubmitValidator.check     deterministic: mode, the account's consent, an allowlisted route with a
                                    deterministic adapter, ownership, the shared readiness rules, the approved digest,
                                    the exact packet files on the page, every required field read back, sensitive
                                    answers confirmed, legal attestations the person approved word for word, no other
                                    claim, no CAPTCHA/OTP/login, the form's fingerprint unchanged
    2. arm                          apply_queue.begin_submission(client="worker") — the same claim MCP and the Apply
                                    kit take — then a SubmissionIntent ARMED, bound to that claim
    3. the page is checked again    (the caller re-reads the form and its values)
    4. commit_clicking              re-reads consent, mode, the claim (CLAIMED, in its lease) and the digest; the
                                    intent becomes CLICKING and the execution SUBMITTING in one commit
    5. the click                    GuardedPage.submit(ticket): the ticket re-reads that CLICKING intent first
    6. after_click                  CONFIRMING, and the canonical state READY_TO_APPLY → APPLYING
    7. ConfirmationVerifier         the adapter's evidence rule; anything less is UNCERTAIN
    8. confirm / uncertain          confirmed → apply_queue.record_submission (the one recorder: APPLYING → APPLIED,
                                    provenance verified_browser); otherwise the claim and the intent are UNCERTAIN
                                    and a person says what happened. Nothing ever clicks again for that claim.

`reconcile` settles an execution whose worker died or was killed: an ARMED intent (nothing was clicked) is aborted
and its claim released; a CLICKING one is UNCERTAIN — never "not submitted", never retried.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    CLICKED_INTENT_STATUSES,
    ApplicationClaim,
    ApplicationExecutionTask,
    Job,
    SubmissionIntent,
)
from app.settings import get_settings

log = logging.getLogger("jobhunt.submission")
ARMED, CLICKING, CONFIRMED, UNCERTAIN, ABORTED = "ARMED", "CLICKING", "CONFIRMED", "UNCERTAIN", "ABORTED"
WORKER_ACTOR = "application_worker"
CONSENT_PATH = ("application", "guarded_auto")
# What a confirmation page says once an application was received (lower-case substrings).
CONFIRMATION_MARKERS = ("thank you for applying", "thanks for applying", "application has been submitted",
                        "application was submitted", "we have received your application",
                        "we've received your application", "application received", "your application has been sent",
                        "successfully submitted", "thank you for your application", "thank you for your interest")
CONFIRMATION_PATHS = ("confirmation", "thank", "submitted", "success")


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def normalize(text: str | None) -> str:
    return " ".join(re.sub(r"[*\s]+", " ", (text or "").lower()).split())


# ───────────── the person's consent ─────────────

def consent(db: Session, user_id: int) -> dict[str, Any]:
    """The account's guarded-submission consent: {enabled, consented_at, attestations}. Re-read at every check."""
    from app.services.user_settings import settings_for

    saved = ((settings_for(db, user_id).preferences or {}).get(CONSENT_PATH[0]) or {}).get(CONSENT_PATH[1]) or {}
    return {"enabled": bool(saved.get("enabled")), "consented_at": saved.get("consented_at"),
            "attestations": [a for a in saved.get("attestations") or [] if isinstance(a, str)]}


def set_consent(db: Session, user_id: int, *, enabled: bool, attestations: list[str] | None = None) -> dict[str, Any]:
    """Turn guarded submission on or off for the account, with the exact attestation texts the person approves.
    Turning it off takes effect at the next check of every run, including one about to click. Does not commit."""
    from sqlalchemy.orm.attributes import flag_modified

    from app.services.user_settings import settings_for

    row = settings_for(db, user_id)
    preferences = dict(row.preferences or {})
    section = dict(preferences.get(CONSENT_PATH[0]) or {})
    texts = sorted({normalize(a)[:500] for a in attestations or [] if normalize(a)})[:50]
    section[CONSENT_PATH[1]] = {"enabled": bool(enabled), "attestations": texts,
                                "consented_at": utcnow().isoformat() if enabled else None}
    preferences[CONSENT_PATH[0]] = section
    row.preferences = preferences
    flag_modified(row, "preferences")
    db.flush()
    return consent(db, user_id)


# ───────────── PreSubmitValidator ─────────────

@dataclass
class Validation:
    ok: bool
    code: str | None = None
    reason: str | None = None
    human: bool = False  # the person must act (a legal attestation, a confirmation), not a failure to retry
    facts: dict[str, Any] = field(default_factory=dict)


def _refuse(code: str, reason: str, *, human: bool = False) -> Validation:
    return Validation(False, code, reason, human=human)


def file_digest(path: str | None) -> str | None:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest() if path else None
    except OSError:
        return None


class PreSubmitValidator:
    """Deterministic, and run twice: before the claim, and again (with `claim`) just before the CLICKING commit."""

    @staticmethod
    def check(db: Session, task: ApplicationExecutionTask, job: Job | None, *, form: Any, page: dict[str, Any],
              files: dict[str, str], expected_digests: dict[str, str], claim: ApplicationClaim | None = None,
              ) -> Validation:
        from app.services import application_execution, apply_queue, approval, ats_forms, ats_map

        settings = get_settings()
        if job is None or task is None or job.id != task.job_id or job.user_id != task.user_id:
            return _refuse("OWNERSHIP", "The execution and the job do not belong together")
        if db.info.get("user_id") not in (None, task.user_id):
            return _refuse("OWNERSHIP", "This run does not belong to the signed-in account")
        if application_execution.mode() != "guarded_auto" or task.mode != "GUARDED_AUTO":
            return _refuse("MODE", "Guarded submission is not enabled on this server")
        if not consent(db, task.user_id)["enabled"]:
            return _refuse("NO_CONSENT", "Guarded submission is off for this account", human=True)
        match = ats_map.for_job(job)
        if match is None or not match.allowed_by(settings.guarded_routes):
            return _refuse("ROUTE_NOT_ALLOWED", "This application's route is not allowlisted for guarded submission")
        if form is None or form.adapter != match.adapter:
            return _refuse("NO_DETERMINISTIC_ADAPTER", "Guarded submission needs the route's own form adapter")
        if (page.get("host") or "") != match.apply_domain:
            return _refuse("WRONG_DOMAIN", "The page is not on the application's own domain")
        if page.get("problem"):
            human = page["problem"] in ats_forms.HUMAN_BLOCKERS
            return _refuse(page["problem"], "The page needs a person", human=human)
        if page.get("fingerprint") != task.form_fingerprint:
            return _refuse("FORM_CHANGED", "The form changed since it was planned")
        reason = apply_queue.not_ready_reason(db, job)
        if reason:
            return _refuse("NOT_READY", reason)
        checked = approval.verify(db, job.application)
        if not checked.ok or (checked.packet.digest if checked.packet else None) != task.packet_digest:
            return _refuse("PACKET_CHANGED", checked.reason or "The approved packet changed since this run started")
        for fmt, digest in expected_digests.items():
            if file_digest(files.get(fmt)) != digest:
                return _refuse("PACKET_FILE_CHANGED", f"The {fmt} file is not the one this run prepared")
        plan = (task.plan or {}).get("fields") or {}
        approved = set(consent(db, task.user_id)["attestations"])
        for f in form.fields:
            entry = plan.get(f.id) or {}
            if f.required and entry.get("status") != "RESOLVED":
                return _refuse("REQUIRED_FIELDS_MISSING", f"A required field has no verified answer ({f.label[:80]})",
                               human=True)
            if entry.get("sensitive") and entry.get("status") == "RESOLVED" and not entry.get("confirmed"):
                return _refuse("SENSITIVE_NOT_CONFIRMED", "A sensitive answer was not confirmed for this run",
                               human=True)
            if f.legal and (f.required or entry.get("status") == "RESOLVED") and normalize(f.label) not in approved:
                return _refuse("LEGAL_ATTESTATION", f"The form asks for an attestation you have not approved: "
                                                    f"{f.label[:200]}", human=True)
        if page.get("missing"):
            return _refuse("REQUIRED_FIELDS_MISSING", "A required field does not hold its answer on the page")
        uploaded = page.get("uploaded") or {}
        for f in form.fields:
            if f.kind == "file" and f.required:
                wanted = Path(files.get("cover_letter" if f.role == "cover_letter" else "pdf") or "").name
                if not wanted or not str(uploaded.get(f.id) or "").endswith(wanted):
                    return _refuse("PACKET_NOT_UPLOADED", "The approved file is not the one attached to the form")
        held = apply_queue.held_claims(db, job)
        others = [c for c in held if claim is None or c.id != claim.id]
        if others:
            return _refuse("CLAIM_CONFLICT", "Another submission of this posting is open, recorded or uncertain")
        if claim is not None and (claim.status != apply_queue.CLAIMED or claim.lease_expires_at <= utcnow()):
            return _refuse("CLAIM_LOST", "This run's claim is no longer open")
        return Validation(True, facts={"route": match.route, "domain": match.apply_domain})


# ───────────── the intent ─────────────

def intents(db: Session, task_id: int) -> list[SubmissionIntent]:
    return list(db.scalars(select(SubmissionIntent).where(SubmissionIntent.execution_task_id == task_id)
                           .order_by(SubmissionIntent.id).execution_options(populate_existing=True)).all())


def clicked(db: Session, task_id: int) -> SubmissionIntent | None:
    """The run's intent that got as far as the click, if any: it is never clicked again."""
    return next((i for i in intents(db, task_id) if i.status in CLICKED_INTENT_STATUSES), None)


@dataclass
class Armed:
    intent: SubmissionIntent | None = None
    claim: ApplicationClaim | None = None
    status: str = "ARMED"
    reason: str | None = None


def arm(db: Session, task: ApplicationExecutionTask, job: Job, *, route: str, domain: str, backend: str,
        file_digests: dict[str, str]) -> Armed:
    """Take the shared claim and record the intent (ARMED). Commits (begin_submission commits the claim)."""
    from app.services import apply_queue

    result = apply_queue.begin_submission(db, job, client="worker", execution_id=task.id)
    if not result.ok:
        return Armed(status=result.status, reason=result.reason)
    claim = result.claim
    intent = SubmissionIntent(user_id=task.user_id, claim_id=claim.id, execution_task_id=task.id, job_id=job.id,
                              status=ARMED, packet_digest=claim.packet_digest, form_fingerprint=task.form_fingerprint,
                              backend=backend, route=route[:240], domain=domain[:255], file_digests=file_digests,
                              created_at=utcnow())
    db.add(intent)
    db.commit()
    return Armed(intent=intent, claim=claim)


def abort(db: Session, intent: SubmissionIntent, job: Job, reason: str) -> None:
    """Give up before the click: the intent is ABORTED and the claim released (nothing left the browser). Only an
    ARMED intent, or a CLICKING one whose click provably never happened (`GuardedPage.submit` refused before it).
    Commits."""
    from app.services import apply_queue

    intent = db.get(SubmissionIntent, intent.id, populate_existing=True)
    claim = db.get(ApplicationClaim, intent.claim_id, populate_existing=True)
    if intent.status not in (ARMED, CLICKING):
        return
    intent.status, intent.resolved_at, intent.reason = ABORTED, utcnow(), reason[:300]
    db.commit()
    if claim is not None and claim.status == apply_queue.CLAIMED:
        try:
            apply_queue.release_claim(db, job, claim.token, reason)
        except ValueError:
            log.warning("claim %s could not be released after an aborted intent", claim.id)


def commit_clicking(db: Session, task: ApplicationExecutionTask, intent: SubmissionIntent) -> Validation:
    """Re-read consent, the mode, the claim and the digest; then CLICKING + SUBMITTING in one commit. The caller holds
    the run's lease in this transaction (effects.within)."""
    from app.services import application_execution, apply_queue, approval

    claim = db.get(ApplicationClaim, intent.claim_id, populate_existing=True)
    job = db.get(Job, task.job_id)
    if application_execution.mode() != "guarded_auto" or not consent(db, task.user_id)["enabled"]:
        return _refuse("NO_CONSENT", "Guarded submission was switched off", human=True)
    if claim is None or claim.status != apply_queue.CLAIMED or claim.lease_expires_at <= utcnow() \
            or claim.execution_id != task.id:
        return _refuse("CLAIM_LOST", "This run's claim is no longer open")
    checked = approval.verify(db, job.application if job else None)
    if not checked.ok or (checked.packet.digest if checked.packet else None) != intent.packet_digest:
        return _refuse("PACKET_CHANGED", "The approved packet changed")
    intent.status, intent.clicking_at = CLICKING, utcnow()
    application_execution.set_stage(task, "SUBMITTING", intent=intent)
    db.commit()
    return Validation(True)


def still_clicking(sessions: Any, user_id: int, task_id: int, intent_id: int) -> bool:
    """The ticket's re-check, on its own session, immediately before the click."""
    from app.services import apply_queue

    with sessions() as db:
        db.info["user_id"] = user_id
        intent = db.get(SubmissionIntent, intent_id)
        task = db.get(ApplicationExecutionTask, task_id)
        claim = db.get(ApplicationClaim, intent.claim_id) if intent is not None else None
        return bool(intent is not None and task is not None and claim is not None and intent.status == CLICKING
                    and intent.execution_task_id == task_id and task.status == "PROCESSING"
                    and claim.status == apply_queue.CLAIMED and claim.lease_expires_at > utcnow())


def after_click(db: Session, task: ApplicationExecutionTask, intent: SubmissionIntent) -> None:
    """The click happened (or may have): CONFIRMING, and the job is APPLYING. Does not commit."""
    from app.services import application_execution
    from app.services.lifecycle import WorkflowState as S
    from app.services.lifecycle import parse_state
    from app.services.work_queue import transition

    application_execution.set_stage(task, "CONFIRMING", intent=intent)
    job = db.get(Job, task.job_id)
    if job is not None and job.state and parse_state(job.state) is S.READY_TO_APPLY:
        transition(db, job, S.APPLYING, actor=WORKER_ACTOR, reason="The final Submit was clicked (guarded run)")


# ───────────── ConfirmationVerifier ─────────────

@dataclass(frozen=True)
class Confirmation:
    confirmed: bool
    facts: dict[str, Any]
    confirmation_id: str | None = None
    reason: str = ""


class ConfirmationVerifier:
    """Whether the site's own evidence shows the application was received. All three of: a successful non-GET response
    from the application's domain after the click, the application form gone from the page, and the site's
    confirmation (a confirmation path, or its wording). A click, a navigation or a success-looking page alone is
    never enough."""

    @staticmethod
    def verify(adapter: str, domain: str, evidence: dict[str, Any]) -> Confirmation:
        responses = [r for r in evidence.get("responses") or [] if r.get("host") == domain]
        posted = [r for r in responses if 200 <= int(r.get("status") or 0) < 400]
        failed = [r for r in responses if int(r.get("status") or 0) >= 400]
        soup = BeautifulSoup(evidence.get("html") or "", "html.parser")
        form_gone = soup.select_one("#application_form" if adapter == "greenhouse" else "form input[type=file]") \
            is None
        text = " ".join(soup.get_text(" ", strip=True).lower().split())[:20000]
        segments = [s for s in re.sub(r"^https?://[^/]+", "", (evidence.get("url") or "").lower().split("?")[0])
                    .split("/") if s]
        worded = any(marker in text for marker in CONFIRMATION_MARKERS)
        at_path = any(p in segment for segment in segments[-2:] for p in CONFIRMATION_PATHS)
        found = re.search(r"(?:confirmation|reference|application)\s*(?:number|id|#)\s*[:#]?\s*([A-Z0-9-]{4,40})",
                          soup.get_text(" ", strip=True), re.IGNORECASE)
        facts = {"responses": [{k: r.get(k) for k in ("method", "status", "host")} for r in responses][:10],
                 "form_gone": form_gone, "confirmation_wording": worded, "confirmation_path": at_path,
                 "final_host": (re.sub(r"^https?://", "", evidence.get("url") or "").split("/")[0])[:200]}
        if failed and not posted:
            return Confirmation(False, facts, reason="The site answered the submission with an error")
        if not posted:
            return Confirmation(False, facts, reason="No successful submission request reached the site")
        if not form_gone:
            return Confirmation(False, facts, reason="The application form is still on the page")
        if not (worded or at_path):
            return Confirmation(False, facts, reason="The site showed no confirmation")
        return Confirmation(True, facts, confirmation_id=found.group(1)[:200] if found else None)


def confirm(db: Session, task: ApplicationExecutionTask, intent: SubmissionIntent, confirmation: Confirmation) -> str:
    """Record the verified submission through the one recorder. Commits. Returns the RecordResult status."""
    from app.services import application_execution, apply_queue

    job = db.get(Job, task.job_id)
    claim = db.get(ApplicationClaim, intent.claim_id, populate_existing=True)
    result = apply_queue.record_submission(db, job, claim_token=claim.token, client="worker",
                                           confirmation_id=confirmation.confirmation_id,
                                           evidence={"kind": "verified_browser", "facts": confirmation.facts})
    task = db.get(ApplicationExecutionTask, task.id, populate_existing=True)
    intent = db.get(SubmissionIntent, intent.id, populate_existing=True)
    if result.status == "SUBMITTED":
        intent.status, intent.resolved_at, intent.evidence = CONFIRMED, utcnow(), confirmation.facts
        application_execution.set_stage(task, "CONFIRMED", intent=intent)
        db.commit()
        return result.status
    # Confirmed on the site but not recordable (the account recorded the posting meanwhile): a person looks.
    uncertain(db, task, intent, f"Confirmed by the site but not recorded: {result.status}", confirmation.facts)
    return result.status


def uncertain(db: Session, task: ApplicationExecutionTask, intent: SubmissionIntent, reason: str,
              facts: dict[str, Any] | None = None) -> None:
    """The outcome of a click is not known: intent and claim UNCERTAIN, the execution SUBMISSION_UNCERTAIN, the job
    APPLYING. Only a person resolves it (apply_queue.resolve_uncertain). Commits."""
    from app.services import application_execution, apply_queue
    from app.services.lifecycle import WorkflowState as S
    from app.services.lifecycle import parse_state
    from app.services.work_queue import transition

    intent = db.get(SubmissionIntent, intent.id, populate_existing=True)
    if intent.status in (CONFIRMED, "RESOLVED_SUBMITTED", "RESOLVED_NOT_SUBMITTED"):
        return
    intent.status, intent.reason = UNCERTAIN, reason[:300]
    intent.evidence = facts or intent.evidence
    apply_queue.mark_uncertain(db, intent.claim_id, reason)
    application_execution.set_stage(task, "SUBMISSION_UNCERTAIN")
    job = db.get(Job, task.job_id)
    if job is not None and job.state and parse_state(job.state) is S.READY_TO_APPLY:
        transition(db, job, S.APPLYING, actor=WORKER_ACTOR, reason="The final Submit may have been clicked")
    db.commit()


def reconcile(db: Session, task: ApplicationExecutionTask, why: str) -> str | None:
    """A guarded run's worker died, its lease ran out or it was killed: settle its intent. ARMED → aborted and the
    claim released (nothing was clicked); CLICKING → UNCERTAIN (it may have been). Returns the intent status it left,
    or None when the run had no intent. Commits."""
    for intent in intents(db, task.id):
        if intent.status == ARMED:
            job = db.get(Job, task.job_id)
            if job is not None:
                abort(db, intent, job, f"{why} before the click")
            return ABORTED
        if intent.status == CLICKING:
            uncertain(db, task, intent, f"{why} after the click started; whether it was submitted is unknown")
            return UNCERTAIN
        if intent.status in (UNCERTAIN, CONFIRMED):
            return intent.status
    return None


def view(intent: SubmissionIntent | None) -> dict[str, Any] | None:
    if intent is None:
        return None
    return {"id": intent.id, "status": intent.status, "route": intent.route, "domain": intent.domain,
            "backend": intent.backend, "reason": intent.reason, "claim_id": intent.claim_id,
            "clicking_at": intent.clicking_at.isoformat() if intent.clicking_at else None,
            "resolved_at": intent.resolved_at.isoformat() if intent.resolved_at else None,
            "evidence": intent.evidence}
