"""One account's Gmail sync: targeted search → deduplicate → match → store → analyse → digest.

Runs from the scheduler's `inbox` job (and POST /api/application-inbox/sync, as a background task), never inside a
request. Every message is committed as soon as it is read, so a failure later in the run (the model, a rate limit,
a crash) loses nothing and the next run carries on. Gmail failures stop this account's sync only.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import ApplicationEmailMessage, GmailConnection
from app.services import gmail, system_log, tracing
from app.services.application_inbox import analysis, digest, matching, queries
from app.services.redaction import redact_text
from app.settings import get_settings

log = logging.getLogger("jobhunt.application_inbox")
LEASE_MINUTES = 30
# Message ids listed per sync, as a multiple of the messages read: listing is cheap (ids only), reading is not.
ID_LISTING_FACTOR = 10
PENDING_BATCH = 50
COUNTERS = ("applications_checked", "queries", "queries_skipped", "messages_skipped", "messages_searched",
            "messages_new", "messages_fetched", "bodies_fetched", "messages_matched", "messages_needs_review", "messages_unmatched", "messages_ignored",
            "deterministic_matches", "llm_assisted_matches", "analyzed", "rule_classifications",
            "llm_classifications", "llm_failures", "schema_failures", "stage_applied", "stage_suggested",
            "followups_suppressed", "pending_retry")


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _take_lease(db: Session, conn: GmailConnection, owner: str, now: datetime) -> bool:
    taken = db.execute(update(GmailConnection).where(
        GmailConnection.id == conn.id,
        (GmailConnection.sync_locked_by.is_(None)) | (GmailConnection.sync_lock_expires_at < now))
        .values(sync_locked_by=owner, sync_lock_expires_at=now + timedelta(minutes=LEASE_MINUTES))
        .execution_options(synchronize_session=False)).rowcount
    db.commit()
    return bool(taken)


def _release(db: Session, conn_id: int, owner: str) -> None:
    db.rollback()
    db.execute(update(GmailConnection).where(GmailConnection.id == conn_id, GmailConnection.sync_locked_by == owner)
               .values(sync_locked_by=None, sync_lock_expires_at=None).execution_options(synchronize_session=False))
    db.commit()


class Progress:
    """Where a sync is, written to gmail_connections.sync_progress for the progress bar. Counts only, never content.

    Phases fill fixed bands of the bar: searching 0–20%, reading messages 20–70%, analysing 70–95%, the digest
    95–100%. Writes are throttled and best effort: progress reporting never fails or slows a sync noticeably."""

    BANDS: ClassVar[dict[str, tuple[int, int]]] = {"starting": (0, 2), "searching": (2, 20), "reading": (20, 70), "analysing": (70, 95),
             "finishing": (95, 99), "done": (100, 100)}
    LABELS: ClassVar[dict[str, str]] = {"starting": "Starting", "searching": "Searching Gmail", "reading": "Reading messages",
              "analysing": "Analysing emails", "finishing": "Writing the digest", "done": "Done"}
    MIN_INTERVAL = 0.5  # seconds between writes within a phase

    def __init__(self, db: Session, conn_id: int | None, started_at: datetime):
        self.db, self.conn_id, self.started_at = db, conn_id, started_at
        self._last_write = 0.0
        self._phase = ""

    def __call__(self, phase: str, done: int = 0, total: int = 0, *, status: str | None = None) -> None:
        if self.conn_id is None:
            return
        now = time.monotonic()
        final = phase == "done" or (total and done >= total)
        if phase == self._phase and not final and now - self._last_write < self.MIN_INTERVAL:
            return
        low, high = self.BANDS[phase]
        percent = low + (high - low) * (min(done, total) / total if total else 0)
        label = self.LABELS[phase] + (f" · {min(done, total)} of {total}" if total else "")
        value = {"phase": phase, "label": label, "done": done, "total": total, "percent": round(percent),
                 "started_at": self.started_at.isoformat(), "updated_at": utcnow().isoformat()}
        if status:
            value["status"] = status
        try:
            self.db.execute(update(GmailConnection).where(GmailConnection.id == self.conn_id)
                            .values(sync_progress=value).execution_options(synchronize_session=False))
            self.db.commit()
        except Exception:  # the bar is a convenience; the sync carries on without it
            self.db.rollback()
            log.debug("could not record inbox sync progress", exc_info=True)
            return
        self._phase, self._last_write = phase, now


def _no_progress(phase: str, done: int = 0, total: int = 0, *, status: str | None = None) -> None:
    return None


def _known_ids(db: Session, ids: list[str]) -> set[str]:
    known: set[str] = set()
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        known |= set(db.scalars(select(ApplicationEmailMessage.gmail_message_id)
                                .where(ApplicationEmailMessage.gmail_message_id.in_(chunk))).all())
    return known


def _search(client: gmail.GmailClient, apps: list[queries.TrackedApplication], last_success: datetime | None,
            now: datetime, limit: int, counters: dict[str, int],
            progress: Callable = _no_progress) -> tuple[dict[str, str], bool]:
    """Message id → thread id for everything the targeted searches (and known threads) return, newest first, and
    whether the listing is complete. Listing ids is cheap (no content), so it covers far more than one sync reads;
    an incomplete listing keeps the checkpoint where it was, so the next sync searches the same window again."""
    found: dict[str, str] = {}
    complete = True
    searches = queries.build_queries(apps, last_success, now)
    for index, query in enumerate(searches):
        if len(found) >= limit:
            complete = False
            break
        progress("searching", index, len(searches))
        counters["queries"] += 1
        wanted = limit - len(found)
        try:
            hits = client.list_message_ids(query, wanted + 1)  # one more than wanted: tells whether more exist
        except gmail.GmailBadRequest:
            counters["queries_skipped"] += 1  # Gmail refuses this search every time: skipping it loses nothing new
            continue
        if len(hits) > wanted:
            complete, hits = False, hits[:wanted]
        for message_id, thread_id in hits:
            found.setdefault(message_id, thread_id)
    for app in apps:
        if not app.watched:
            continue
        for thread_id in sorted(app.known_threads):
            if len(found) >= limit:
                complete = False
                break
            for message_id, thread in client.thread_message_ids(thread_id):
                found.setdefault(message_id, thread)
    counters["messages_searched"] = len(found)
    return found, complete


def _store(db: Session, meta: gmail.EmailMeta, body: str, result: matching.MatchResult) -> ApplicationEmailMessage | None:
    """Save one message with the least content its match needs. Returns None when another sync saved it first."""
    # Linked (or review-band) mail keeps what a person and the analysis need; anything else keeps its ids only —
    # enough to never read it again, and nothing about what it said or who sent it.
    keep = result.status in ("MATCHED", "NEEDS_REVIEW")
    row = ApplicationEmailMessage(
        gmail_message_id=meta.message_id, gmail_thread_id=meta.thread_id,
        sender=meta.sender if keep else "", sender_domain=meta.sender_domain if keep else "",
        subject=meta.subject if keep else "", received_at=meta.received_at if keep else None,
        snippet=meta.snippet if keep else "", relevant_text=body if keep else "",
        automated=meta.automated, application_id=result.application_id if result.status == "MATCHED" else None,
        match_score=result.score, match_signals=result.signals or None, match_method=result.method,
        match_status=result.status, match_candidates=result.candidates if result.status == "NEEDS_REVIEW" else None,
        analysis_status="PENDING" if result.status == "MATCHED" else "SKIPPED", attempts=0)
    try:
        with db.begin_nested():
            db.add(row)
    except IntegrityError:
        return None  # deduplicated: the unique (user, message) constraint is the final word
    db.commit()
    return row


def _read_message(db: Session, client: gmail.GmailClient, message_id: str, own_address: str,
                  apps: list[queries.TrackedApplication], counters: dict[str, int]) -> ApplicationEmailMessage | None:
    settings = get_settings()
    meta = gmail.parse_meta(client.get_metadata(message_id))
    counters["messages_fetched"] += 1
    if not meta.message_id:
        return None
    if own_address and meta.sender_email == own_address.lower():
        result = matching.MatchResult(None, 0.0, [], "IGNORED", "NONE", reason="sent by you")
        return _store(db, meta, "", result)
    body = ""
    if matching.headers_point_at_application(meta, apps):  # only mail whose headers point at an application is opened
        body = gmail.body_text(client.get_full(message_id), max(500, settings.application_inbox_body_chars))
        counters["bodies_fetched"] += 1
    result = matching.match(meta, body, apps)
    row = _store(db, meta, body, result)
    if row is not None and row.match_status == "NEEDS_REVIEW":
        _assist(db, row, counters)
    return row


def _assist(db: Session, row: ApplicationEmailMessage, counters: dict[str, int]) -> None:
    """Review band: the model may confirm the deterministic favourite. It can never pick an application the rules
    did not propose, and anything short of a confident agreement is left for a person."""
    settings = get_settings()
    candidates = row.match_candidates or []
    top = candidates[0] if candidates else None
    chosen, confidence = analysis.disambiguate(db, row, candidates)
    if (top and chosen == top["application_id"] and confidence >= settings.application_email_auto_match_threshold
            and (row.match_score or 0) >= settings.application_email_review_threshold):
        row.application_id, row.match_status, row.match_method = chosen, "MATCHED", "LLM_ASSISTED"
        row.analysis_status = "PENDING"
        row.match_signals = [*(row.match_signals or []), "llm_confirmed"]
        counters["llm_assisted_matches"] += 1
    db.commit()


def _review_event(db: Session, row: ApplicationEmailMessage) -> None:
    """An email whose application is uncertain is shown for review, with its candidates and no stage effect."""
    rule = analysis.classify.match_rule(row.subject, row.relevant_text or row.snippet)
    reading = (analysis.classify.rule_analysis(rule, needs_review=True) if rule
               else analysis.classify.unknown_analysis(row.automated))
    analysis.save_event(db, row, None, reading, method="RULE", model=None, prompt_version=None, explicit=False)
    db.commit()


def _count_failure(db: Session, message_id: int, now: datetime, exc: Exception) -> None:
    """An unexpected analysis error is an attempt like a model failure: retried with backoff, then left FAILED (the
    message itself is kept). Without this, the same error would re-run — and re-bill — the analysis every sync."""
    settings = get_settings()
    try:
        row = db.get(ApplicationEmailMessage, message_id)
        if row is None:
            return
        row.attempts += 1
        row.last_error = redact_text(f"{type(exc).__name__}: {exc}", 1000)
        if row.attempts >= max(1, settings.application_inbox_analysis_max_attempts):
            row.analysis_status, row.next_attempt_at = "FAILED", None
        else:
            row.next_attempt_at = now + timedelta(seconds=settings.application_inbox_retry_seconds * row.attempts)
        db.commit()
    except Exception:  # noqa: BLE001  bookkeeping must never stop the sync
        db.rollback()


def process_pending(db: Session, now: datetime, counters: dict[str, int], limit: int = PENDING_BATCH,
                    progress: Callable = _no_progress) -> None:
    """Analyse matched messages waiting for it (new ones, and earlier model failures whose retry time has come)."""
    rows = db.scalars(select(ApplicationEmailMessage).where(
        ApplicationEmailMessage.analysis_status == "PENDING",
        (ApplicationEmailMessage.next_attempt_at.is_(None)) | (ApplicationEmailMessage.next_attempt_at <= now))
        .order_by(ApplicationEmailMessage.received_at, ApplicationEmailMessage.id).limit(limit)).all()
    for index, row in enumerate(rows):
        progress("analysing", index, len(rows))
        try:
            with tracing.trace("application_inbox_analysis", user_id=row.user_id):
                outcome = analysis.process_message(db, row, now=now, counters=counters)
        except Exception as exc:  # one message failing must not stop the others
            db.rollback()
            log.warning("inbox analysis failed for message %s", row.id, exc_info=True)
            _count_failure(db, row.id, now, exc)
            continue
        if outcome == "retrying":
            counters["pending_retry"] += 1
    if rows:
        progress("analysing", len(rows), len(rows))


def sync_account(sessions: Callable[[], Session], user_id: int, *, trigger: str = "schedule",
                 worker_id: str = "inbox", now: datetime | None = None) -> dict[str, Any]:
    """Sync one account. Returns a JSON-safe summary; never raises for a Gmail, OAuth or model problem."""
    now = now or utcnow()
    settings = get_settings()
    counters = dict.fromkeys(COUNTERS, 0)
    started = time.monotonic()
    summary: dict[str, Any] = {"user_id": user_id, "trigger": trigger, "status": "succeeded"}
    with sessions() as db, tracing.trace("application_inbox", user_id=user_id):
        db.info["user_id"] = user_id
        conn = gmail.connection_for(db, user_id)
        if conn is None or conn.status != gmail.CONNECTED or not conn.refresh_token_enc:
            process_pending(db, now, counters)  # model retries do not need Gmail
            return {**summary, "status": "skipped", "reason": "Gmail is not connected", **counters}
        owner = f"{worker_id}:{trigger}"
        if not _take_lease(db, conn, owner, now):
            return {**summary, "status": "skipped", "reason": "another sync of this account is running"}
        conn_id = conn.id
        progress = Progress(db, conn_id, now)
        progress("starting")
        try:
            summary.update(_run(db, conn, now, counters, progress))
            summary["duration_ms"] = int((time.monotonic() - started) * 1000)
            run = {**counters, "at": now.isoformat(), "trigger": trigger, "status": summary["status"],
                   "error": summary.get("error"), "duration_ms": summary["duration_ms"]}
            progress("finishing")
            if summary["status"] in ("succeeded", "partial"):
                digest.build(db, user_id, now, run)
            else:
                digest.record_run(db, user_id, now, run)
            db.commit()
        finally:
            progress("done", status=summary["status"])
            _release(db, conn_id, owner)
    level = "INFO" if summary["status"] in ("succeeded", "partial") else "WARNING"
    system_log.log_event(message=f"Application inbox sync {summary['status']}: {counters['messages_new']} new, "
                                 f"{counters['messages_matched']} matched, {counters['stage_applied']} stage changes",
                         category="EMAIL", level=level, provider="GMAIL", operation="inbox_sync", user_id=user_id,
                         status=summary["status"], duration_ms=summary.get("duration_ms"),
                         details={k: v for k, v in {**counters, "error": summary.get("error")}.items() if v})
    return {**summary, **counters, "limit": settings.gmail_max_messages_per_sync}


def _run(db: Session, conn: GmailConnection, now: datetime, counters: dict[str, int],
         progress: Callable = _no_progress) -> dict[str, Any]:
    settings = get_settings()
    conn.last_sync_at = now
    db.commit()
    apps = queries.tracked_applications(db)
    watched = [a for a in apps if a.watched]
    counters["applications_checked"] = len(watched)
    status, error = "succeeded", None
    if watched:
        try:
            token = gmail.access_token(db, conn)
            with gmail.GmailClient(token, conn.user_id) as client:
                per_sync = max(1, settings.gmail_max_messages_per_sync)
                found, listed_all = _search(client, apps, conn.last_successful_sync_at, now,
                                            per_sync * ID_LISTING_FACTOR, counters, progress)
                known = _known_ids(db, list(found))
                new = [mid for mid in found if mid not in known]
                # At most GMAIL_MAX_MESSAGES_PER_SYNC are read per run; the rest wait for the next one, which finds
                # them again because the checkpoint does not move until a run has read everything it found.
                complete = listed_all and len(new) <= per_sync
                new = new[:per_sync]
                counters["messages_new"] = len(new)
                for index, message_id in enumerate(new):
                    progress("reading", index, len(new))
                    try:
                        row = _read_message(db, client, message_id, conn.email_address, apps, counters)
                    except gmail.GmailBadRequest:
                        db.rollback()
                        counters["messages_skipped"] += 1  # unreadable: not stored, and the checkpoint waits for it
                        complete = False
                        continue
                    if row is None:
                        continue
                    key = {"MATCHED": "messages_matched", "NEEDS_REVIEW": "messages_needs_review",
                           "UNMATCHED": "messages_unmatched", "IGNORED": "messages_ignored"}[row.match_status]
                    counters[key] += 1
                    if row.match_status == "MATCHED" and row.match_method == "DETERMINISTIC":
                        counters["deterministic_matches"] += 1
                    if row.match_status == "NEEDS_REVIEW":
                        _review_event(db, row)
                progress("reading", len(new), len(new))
                conn.history_id = str(client.profile().get("historyId") or "")[:40] or conn.history_id
                if not complete:
                    status = "partial"
        except gmail.GmailConnectionRequired as exc:
            db.rollback()
            gmail.mark(db, conn, gmail.CONNECTION_REQUIRED, str(exc))
            status, error = "connection_required", str(exc)
        except gmail.GmailRateLimited as exc:
            db.rollback()
            status, error = "rate_limited", str(exc)
        except gmail.GmailError as exc:
            db.rollback()
            status, error = "failed", str(exc)
    process_pending(db, now, counters, progress=progress)
    if status == "succeeded":
        conn.last_successful_sync_at, conn.last_error = now, None
    elif status == "partial":
        conn.last_error = None  # nothing wrong: more mail than one run reads; the next run carries on from here
    else:
        conn.last_error = (error or status)[:1000]
    db.commit()
    return {"status": status, "error": error}
