"""The derived embedding index over an account's verified evidence and saved answers.

    Evidence / KnowledgeAnswer rows (authoritative)
        │  flush listener (here): an edit marks the row's entries PENDING, an un-verify DEACTIVATED, a delete removes
        │  them with their vectors — in the same transaction, metadata only, no model call inside a request
        ▼
    rag_index_entries  (one per source row and index version)  ──  rag_index job (scheduler) claims PENDING ones,
        │                                                            embeds the live text, stores the vector under its
        ▼                                                            content hash (lease-guarded), FAILED → retried
    rag_vectors (app.services.vector_store)

    sweep(): the safety net that diffs live rows against entries (new rows, missed edits, deletions).
    Versions: one per embedding model × vector size × document format (app.services.embeddings.EmbeddingModel). A new
    one is BUILDING beside the SERVING one and replaces it only once it covers the eligible rows
    (RAG_BACKFILL_MIN_COVERAGE); the old one is RETIRED and purged after RAG_RETIRED_RETENTION_DAYS.

Nothing here decides what a job may use (app.services.evidence_tags) or what is retrieved (app.services.rag_retrieval):
a stale or missing vector only costs recall, because retrieval checks the live row and its hash every time.
"""
from __future__ import annotations

import hashlib
import logging
import math
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, delete, event, func, or_, select, text, update
from sqlalchemy.orm import Session, attributes

from app.models import (
    Evidence,
    EvidenceResumeLink,
    KnowledgeAnswer,
    RagIndexEntry,
    RagIndexVersion,
    Resume,
    now,
)
from app.services import backoff, server_settings, vector_store, worker_settings
from app.services.embeddings import (
    EmbeddingError,
    EmbeddingModel,
    EmbeddingService,
    EmbeddingUnavailable,
)
from app.services.lifecycle import STOPPED_BY_USER
from app.settings import get_settings

log = logging.getLogger("jobhunt.rag_index")

EVIDENCE, ANSWER = "evidence", "knowledge_answer"
KIND = "indexing"  # its Running workers kind (app.services.workers) and worker settings (app.services.worker_settings)
COLLECTION = {EVIDENCE: "CANDIDATE_EVIDENCE", ANSWER: "ATS_ANSWER"}
LEASE_SECONDS = 300
# The fields whose change changes what a source says; anything else (use counts, timestamps) is not an edit.
WATCHED = {Evidence: ("claim", "company", "role", "skills", "verified", "source"),
           KnowledgeAnswer: ("question", "answer", "verified", "sensitive")}
SOURCE_TYPE = {Evidence: EVIDENCE, KnowledgeAnswer: ANSWER}


# ───────────── what is embedded ─────────────

def evidence_text(row: Evidence) -> str:
    skills = ", ".join(str(s) for s in (row.skills or [])[:12])
    return "\n".join(part for part in (row.claim or "", f"Role: {row.role}" if row.role else "",
                                       f"Company: {row.company}" if row.company else "",
                                       f"Skills: {skills}" if skills else "") if part)


def answer_text(row: KnowledgeAnswer) -> str:
    """A sensitive answer (work authorization, sponsorship …) is never sent to an embedding model: its question is."""
    if row.sensitive:
        return f"Question: {row.question or ''}"
    return f"Question: {row.question or ''}\nAnswer: {row.answer or ''}"


def content_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def eligible_evidence(row: Evidence | None) -> bool:
    return row is not None and bool(row.verified) and bool((row.claim or "").strip())


def eligible_answer(row: KnowledgeAnswer | None) -> bool:
    return row is not None and bool(row.verified) and bool((row.question or "").strip()) and \
        (bool(row.sensitive) or bool((row.answer or "").strip()))


def live_sources(db: Session, user_id: int) -> dict[tuple[str, int], tuple[str, bool, str]]:
    """(source type, id) → (text, sensitive, collection) for every row the index should cover."""
    out: dict[tuple[str, int], tuple[str, bool, str]] = {}
    for row in db.scalars(select(Evidence).where(Evidence.user_id == user_id, Evidence.verified.is_(True))).all():
        if eligible_evidence(row):
            out[(EVIDENCE, row.id)] = (evidence_text(row), False, COLLECTION[EVIDENCE])
    for row in db.scalars(select(KnowledgeAnswer).where(KnowledgeAnswer.user_id == user_id,
                                                        KnowledgeAnswer.verified.is_(True))).all():
        if eligible_answer(row):
            out[(ANSWER, row.id)] = (answer_text(row), bool(row.sensitive), COLLECTION[ANSWER])
    return out


# ───────────── flush listener: edits, verification changes and deletions ─────────────

def _changed(obj: Any) -> bool:
    state = attributes.instance_state(obj)
    return any(state.attrs[name].history.has_changes() for name in WATCHED[type(obj)] if name in state.attrs)


@event.listens_for(Session, "before_flush")
def _collect(session: Session, _flush_context, _instances) -> None:
    try:
        pending = session.info.setdefault("rag_changes", {"edited": set(), "unverified": set(), "deleted": set(),
                                                          "resumes": set()})
        for obj in session.dirty:
            if type(obj) in WATCHED and obj.id is not None and _changed(obj):
                key = (obj.user_id, SOURCE_TYPE[type(obj)], obj.id)
                (pending["unverified"] if not obj.verified else pending["edited"]).add(key)
        for obj in session.deleted:
            if type(obj) in WATCHED and obj.id is not None:
                pending["deleted"].add((obj.user_id, SOURCE_TYPE[type(obj)], obj.id))
            elif isinstance(obj, Resume) and obj.id is not None:
                pending["resumes"].add(obj.id)
    except Exception:  # the index is derived: never let it change a person's action
        log.warning("could not record index changes", exc_info=True)


@event.listens_for(Session, "after_soft_rollback")
def _forget(session: Session, _previous_transaction) -> None:
    session.info.pop("rag_changes", None)  # a flush that rolled back changed nothing


@event.listens_for(Session, "after_flush")
def _apply(session: Session, _flush_context) -> None:
    pending = session.info.pop("rag_changes", None)
    if not pending or not any(pending.values()):
        return
    try:
        conn = session.connection()
        with conn.begin_nested():
            apply_changes(conn, pending)
    except Exception:  # a sweep repairs anything missed; the person's change stands
        log.warning("could not update the retrieval index after a change", exc_info=True)


def _not_stopped(columns: Any) -> Any:
    return or_(columns.last_error.is_(None), columns.last_error != STOPPED_BY_USER)


def apply_changes(conn: Any, pending: dict[str, set]) -> None:
    stamp = now()
    entries = RagIndexEntry.__table__
    for user_id, source_type, source_id in pending["deleted"]:
        ids = list(conn.execute(select(entries.c.id).where(entries.c.user_id == user_id,
                                                           entries.c.source_type == source_type,
                                                           entries.c.source_id == source_id)).scalars())
        vector_store.delete_entries(conn, ids)
        if ids:
            conn.execute(delete(entries).where(entries.c.id.in_(ids)))
        if source_type == EVIDENCE:  # its tags (cascaded on PostgreSQL; SQLite does not enforce foreign keys)
            links = EvidenceResumeLink.__table__
            conn.execute(delete(links).where(links.c.evidence_id == source_id))
    for status, keys in (("PENDING", pending["edited"]), ("DEACTIVATED", pending["unverified"])):
        for user_id, source_type, source_id in keys:
            where = [entries.c.user_id == user_id, entries.c.source_type == source_type,
                     entries.c.source_id == source_id]
            values: dict[str, Any] = {}
            if status == "DEACTIVATED":  # a withdrawn fact keeps no vector made from its text
                vector_store.delete_entries(conn, list(conn.execute(select(entries.c.id).where(*where)).scalars()))
                values = {"content_hash": None, "embedded_at": None}
            else:  # an entry a person stopped stays stopped until they retry it (Running workers)
                where.append(_not_stopped(entries.c))
            # Clearing the lease makes an indexer that read the old text unable to store its vector.
            conn.execute(update(entries).where(*where).values(
                status=status, lock_token=None, lock_expires_at=None, next_attempt_at=None, attempt_count=0,
                deactivated_at=stamp if status == "DEACTIVATED" else None,
                completed_at=stamp if status == "DEACTIVATED" else None, updated_at=stamp, **values))
    if pending["resumes"]:
        links = EvidenceResumeLink.__table__
        conn.execute(delete(links).where(links.c.resume_id.in_(list(pending["resumes"]))))


# ───────────── versions ─────────────

def serving_version(db: Session, user_id: int) -> RagIndexVersion | None:
    return db.scalar(select(RagIndexVersion).where(RagIndexVersion.user_id == user_id,
                                                   RagIndexVersion.status == "SERVING"))


def ensure_version(db: Session, user_id: int, model: EmbeddingModel) -> RagIndexVersion:
    """The index version for the configured model (created BUILDING when new). A BUILDING version of a model that is no
    longer configured is abandoned (RETIRED); a SERVING one keeps serving until the new one is complete."""
    version = db.scalar(select(RagIndexVersion).where(RagIndexVersion.user_id == user_id,
                                                      RagIndexVersion.identity_hash == model.identity_hash))
    for other in db.scalars(select(RagIndexVersion).where(RagIndexVersion.user_id == user_id,
                                                          RagIndexVersion.status == "BUILDING",
                                                          RagIndexVersion.identity_hash != model.identity_hash)):
        other.status, other.retired_at = "RETIRED", now()
    if version is None:
        version = RagIndexVersion(user_id=user_id, identity_hash=model.identity_hash, provider=model.provider,
                                  model=model.model, underlying_model=model.underlying_model,
                                  dimensions=model.dimensions, doc_format_version=model.doc_format_version,
                                  status="BUILDING", created_at=now(), updated_at=now())
        db.add(version)
    elif version.status == "RETIRED":  # the configuration went back to an older model: rebuild that index
        version.status, version.retired_at = "BUILDING", None
    db.flush()
    return version


def sweep(db: Session, user_id: int, version: RagIndexVersion) -> int:
    """Bring the version's entries in line with the live rows; returns how many rows are eligible."""
    live = live_sources(db, user_id)
    entries = {(e.source_type, e.source_id): e for e in db.scalars(
        select(RagIndexEntry).where(RagIndexEntry.index_version_id == version.id)).all()}
    stamp = now()
    for key, (body, sensitive, collection) in live.items():
        entry = entries.get(key)
        if entry is None:
            db.add(RagIndexEntry(user_id=user_id, index_version_id=version.id, collection=collection,
                                 source_type=key[0], source_id=key[1], sensitive=sensitive,
                                 verification_status="VERIFIED", status="PENDING", created_at=stamp, updated_at=stamp))
        elif entry.status in ("ACTIVE", "DEACTIVATED") and entry.content_hash != content_hash(body):
            entry.status, entry.sensitive, entry.verification_status = "PENDING", sensitive, "VERIFIED"
            entry.deactivated_at, entry.attempt_count, entry.updated_at = None, 0, stamp
            entry.next_attempt_at = entry.completed_at = None
    gone, withdrawn = [], []
    for key, entry in entries.items():
        if key in live:
            continue
        exists = db.get(Evidence if key[0] == EVIDENCE else KnowledgeAnswer, key[1]) is not None
        if not exists:
            gone.append(entry.id)
        else:
            withdrawn.append(entry.id)  # also repairs a deactivation whose vector was left behind
            if entry.status != "DEACTIVATED":
                entry.status, entry.verification_status = "DEACTIVATED", "UNVERIFIED"
                entry.deactivated_at, entry.completed_at, entry.updated_at = stamp, stamp, stamp
                entry.lock_token = entry.lock_expires_at = entry.next_attempt_at = None
            entry.content_hash, entry.embedded_at = None, None
    if withdrawn:
        vector_store.delete_entries(db, withdrawn)
    if gone:
        vector_store.delete_entries(db, gone)
        db.execute(delete(RagIndexEntry).where(RagIndexEntry.id.in_(gone)))
    db.flush()
    return len(live)


# ───────────── embedding work ─────────────
# The indexer's queue is a Running workers kind ("indexing", app.services.workers): a failure waits as PENDING with a
# retry time (RETRYING) until the account's attempts run out (FAILED); Kill ends an entry FAILED with
# lifecycle.STOPPED_BY_USER, and nothing claims it again until a person retries it; Run now (run_now_at) is claimed
# first, past the retry wait, and its calls skip the provider cooldown (backoff.bypass); an account that paused the
# worker only has its run-now entries claimed. Every write after the model call is lease-guarded (`_guarded`).

@dataclass
class Claimed:
    token: str
    entries: list[tuple[int, str, int]]  # (entry id, source type, source id)
    run_now: bool = False

    @property
    def task_id(self) -> str:
        """How scheduled_jobs.run_live tells claims apart: the batch's lease token."""
        return self.token


def claim(db: Session, user_id: int, version_id: int, limit: int, *, at: datetime) -> Claimed | None:
    """Lease up to `limit` due entries of one index version. Run-now entries go first, in a batch of their own."""
    E = RagIndexEntry
    own = worker_settings.for_user(db, user_id, KIND)
    base = (E.user_id == user_id, E.index_version_id == version_id)
    abandoned = and_(E.status == "PROCESSING", E.lock_expires_at < at)
    # A run that died with the entry's last attempt: it is failed, not tried again.
    db.execute(update(E).where(*base, abandoned, E.attempt_count >= own["max_attempts"]).values(
        status="FAILED", lock_token=None, lock_expires_at=None, completed_at=at, updated_at=at,
        last_error="LEASE_EXPIRED: the indexer stopped before this entry finished")
        .execution_options(synchronize_session=False))
    urgent = and_(E.run_now_at.is_not(None), or_(E.status == "PENDING", abandoned))
    normal = or_(and_(E.status == "PENDING", or_(E.next_attempt_at.is_(None), E.next_attempt_at <= at)), abandoned)
    for due, run_now in ((urgent, True), (normal, False)):
        if not run_now and not own["auto"]:
            break  # paused: the queue waits, nothing is dropped
        query = select(E.id).where(*base, due).order_by(E.id).limit(limit)
        if db.get_bind().dialect.name == "postgresql":
            query = query.with_for_update(skip_locked=True)
        ids = list(db.scalars(query).all())
        if not ids:
            continue
        token = str(uuid.uuid4())
        db.execute(update(E).where(E.id.in_(ids), due).values(
            status="PROCESSING", lock_token=token, lock_expires_at=at + timedelta(seconds=LEASE_SECONDS),
            attempt_count=E.attempt_count + 1, started_at=at, completed_at=None, run_now_at=None, updated_at=at)
            .execution_options(synchronize_session=False))
        rows = db.execute(select(E.id, E.source_type, E.source_id).where(E.lock_token == token)).all()
        db.commit()
        if rows:
            return Claimed(token, [(r[0], r[1], r[2]) for r in rows], run_now)
    db.commit()
    return None


def renew(sessions, claims: list[Claimed]) -> set[str]:
    """A scheduled_jobs.run_live heartbeat: extend the lease of every batch still held; returns their tokens. A batch
    whose entries were all stopped (Kill) or edited is no longer held, so the pass stops waiting for it."""
    held: set[str] = set()
    until = now() + timedelta(seconds=LEASE_SECONDS)
    with sessions() as db:
        for c in claims:
            won = db.execute(update(RagIndexEntry).where(RagIndexEntry.lock_token == c.token,
                                                         RagIndexEntry.status == "PROCESSING")
                             .values(lock_expires_at=until).execution_options(synchronize_session=False))
            if won.rowcount:
                held.add(c.token)
        db.commit()
    return held


def _guarded(db: Session, entry_id: int, token: str, **values: Any) -> bool:
    result = db.execute(update(RagIndexEntry).where(RagIndexEntry.id == entry_id, RagIndexEntry.lock_token == token)
                        .values(lock_token=None, lock_expires_at=None, updated_at=now(), **values)
                        .execution_options(synchronize_session=False))
    return result.rowcount == 1


def process(sessions, user_id: int, service: EmbeddingService, version_id: int, dimensions: int,
            claimed: Claimed) -> dict[str, int]:
    """Embed one claimed batch. No transaction is held across the model call; every write is lease-guarded."""
    out = {"embedded": 0, "reused": 0, "deactivated": 0, "removed": 0, "failed": 0, "lost_lease": 0}
    with sessions() as db:
        db.info["user_id"] = user_id
        live = live_sources(db, user_id)
        stored = vector_store.stored_hashes(db, user_id, [e[0] for e in claimed.entries])
        todo: list[tuple[int, str, str]] = []
        for entry_id, source_type, source_id in claimed.entries:
            found = live.get((source_type, source_id))
            if found is None:
                exists = db.get(Evidence if source_type == EVIDENCE else KnowledgeAnswer, source_id) is not None
                if exists:
                    if _guarded(db, entry_id, claimed.token, status="DEACTIVATED", verification_status="UNVERIFIED",
                                deactivated_at=now(), completed_at=now(), content_hash=None, embedded_at=None):
                        vector_store.delete_entries(db, [entry_id])
                        out["deactivated"] += 1
                else:
                    vector_store.delete_entries(db, [entry_id])
                    db.execute(delete(RagIndexEntry).where(RagIndexEntry.id == entry_id,
                                                           RagIndexEntry.lock_token == claimed.token))
                    out["removed"] += 1
                continue
            digest = content_hash(found[0])
            if stored.get(entry_id) == digest:  # e.g. re-verified without an edit: the vector is still right
                out["reused"] += _guarded(db, entry_id, claimed.token, status="ACTIVE", content_hash=digest,
                                          verification_status="VERIFIED", deactivated_at=None, last_error=None,
                                          next_attempt_at=None, completed_at=now())
                continue
            todo.append((entry_id, found[0], digest))
        db.commit()
    if not todo:
        return out
    try:
        with backoff.bypass(claimed.run_now):  # a person's Run now skips the provider cooldown for this one batch
            result = service.embed_documents([body for _, body, _ in todo])
    except EmbeddingError as exc:
        _fail(sessions, user_id, claimed.token, [e for e, _, _ in todo], exc)
        out["failed"] += len(todo)
        if isinstance(exc, EmbeddingUnavailable) or exc.code in ("RATE_LIMITED", "UNAVAILABLE", "AUTH",
                                                                  "DIMENSION_MISMATCH"):
            exc.partial = out  # type: ignore[attr-defined]  what this batch did before the provider failed
            raise
        return out
    with sessions() as db:
        db.info["user_id"] = user_id
        rows = []
        for (entry_id, _body, digest), vector in zip(todo, result.vectors, strict=True):
            if _guarded(db, entry_id, claimed.token, status="ACTIVE", content_hash=digest, embedded_at=now(),
                        verification_status="VERIFIED", deactivated_at=None, last_error=None, next_attempt_at=None,
                        completed_at=now()):
                rows.append({"entry_id": entry_id, "user_id": user_id, "index_version_id": version_id,
                             "dimensions": dimensions, "content_hash": digest, "embedding": vector})
                out["embedded"] += 1
            else:
                out["lost_lease"] += 1  # edited, deleted, stopped or reclaimed meanwhile
        vector_store.upsert(db, rows)
        db.commit()
    return out


def _fail(sessions, user_id: int, token: str, entry_ids: list[int], exc: EmbeddingError) -> None:
    """Back off and retry while the account's attempts last (PENDING with a retry time), then FAILED."""
    error = f"{exc.code}: {exc}"[:300]
    with sessions() as db:
        db.info["user_id"] = user_id
        own = worker_settings.for_user(db, user_id, KIND)
        for entry_id in entry_ids:
            attempts = db.scalar(select(RagIndexEntry.attempt_count).where(RagIndexEntry.id == entry_id)) or 1
            if attempts >= own["max_attempts"]:
                _guarded(db, entry_id, token, status="FAILED", last_error=error, next_attempt_at=None,
                         completed_at=now())
            else:
                wait = min(3600, own["retry_seconds"] * 2 ** min(attempts - 1, 7))
                _guarded(db, entry_id, token, status="PENDING", last_error=error,
                         next_attempt_at=now() + timedelta(seconds=wait))
        db.commit()


def refresh_counts(db: Session, version: RagIndexVersion, eligible: int) -> None:
    version.eligible_count = eligible
    version.embedded_count = db.scalar(select(func.count(RagIndexEntry.id)).where(
        RagIndexEntry.index_version_id == version.id, RagIndexEntry.status == "ACTIVE")) or 0
    version.failed_count = db.scalar(select(func.count(RagIndexEntry.id)).where(
        RagIndexEntry.index_version_id == version.id, RagIndexEntry.status == "FAILED")) or 0
    version.updated_at = now()


def maybe_serve(db: Session, user_id: int, version: RagIndexVersion) -> bool:
    """Switch retrieval to `version` once it covers enough of the eligible rows. True when it switched."""
    if version.status != "BUILDING":
        return False
    coverage = min(1.0, max(0.0, float(get_settings().rag_backfill_min_coverage)))
    needed = math.ceil(coverage * version.eligible_count)
    if version.embedded_count < needed:
        return False
    old = serving_version(db, user_id)
    if old is not None:
        old.status, old.retired_at, old.updated_at = "RETIRED", now(), now()
        db.flush()  # one SERVING version per account (partial unique index)
    version.status, version.serving_at, version.last_error = "SERVING", now(), None
    db.flush()
    return True


@dataclass
class AccountPass:
    """One account's indexing pass: what `prepare` found, what is left of its budget, and what it did so far."""

    user_id: int
    service: EmbeddingService
    version_id: int
    dimensions: int
    budget: int
    totals: dict[str, Any]
    error: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


def prepare(sessions, user_id: int, *, service: EmbeddingService | None = None) -> AccountPass | dict[str, Any]:
    """Sweep the account's entries into line with its rows (metadata only, also while the worker is paused: the queue
    is kept, never dropped). A dict says why the account has nothing to index."""
    with sessions() as db:
        db.info["user_id"] = user_id
        if service is None:
            try:
                service = EmbeddingService.for_user(db, user_id)
            except EmbeddingUnavailable as exc:
                return {"user_id": user_id, "skipped": exc.code, "reason": str(exc)}
        ok, reason = vector_store.setup(db.get_bind())
        if not ok:
            return {"user_id": user_id, "skipped": "VECTOR_UNAVAILABLE", "reason": reason}
        from app.services import evidence_tags

        evidence_tags.backfill(db, user_id)
        version = ensure_version(db, user_id, service.identity)
        sweep(db, user_id, version)
        version_id, dimensions = version.id, version.dimensions
        db.commit()
    totals: dict[str, Any] = {"user_id": user_id, "index_version": version_id, "embedded": 0, "reused": 0,
                              "deactivated": 0, "removed": 0, "failed": 0, "lost_lease": 0}
    return AccountPass(user_id, service, version_id, dimensions,
                       max(1, server_settings.value("rag_index_max_entries_per_run")), totals)


def take(sessions, run: AccountPass) -> Claimed | None:
    """The account's next batch, or None once its budget is spent, the provider failed or nothing is due."""
    with run._lock:
        if run.error or run.budget <= 0:
            return None
        size = min(max(1, server_settings.value("rag_embed_batch_size")), run.budget)
        with sessions() as db:
            db.info["user_id"] = run.user_id
            claimed = claim(db, run.user_id, run.version_id, size, at=now())
        if claimed is not None:
            run.budget -= len(claimed.entries)
        return claimed


def work(sessions, run: AccountPass, claimed: Claimed) -> str:
    """Embed one batch of the pass. A provider failure stops the pass's further claims; its entries back off."""
    try:
        done = process(sessions, run.user_id, run.service, run.version_id, run.dimensions, claimed)
        outcome = "embedded"
    except EmbeddingError as exc:  # the provider is unavailable for now: stop, the entries are retried later
        done, outcome = getattr(exc, "partial", {}), "provider_error"
        with run._lock:
            run.error = run.error or f"{exc.code}: {exc}"[:300]
    with run._lock:
        for key, value in done.items():
            run.totals[key] += value
    return outcome


def finish(sessions, run: AccountPass) -> dict[str, Any]:
    with sessions() as db:
        db.info["user_id"] = run.user_id
        version = db.get(RagIndexVersion, run.version_id)
        # Coverage is judged against the rows as they are now, not as they were before the embedding calls: a fact
        # verified or added meanwhile gets its PENDING entry here and keeps an incomplete version from serving.
        refresh_counts(db, version, sweep(db, run.user_id, version))
        if run.error:
            version.last_error = run.error
        totals = dict(run.totals)
        totals["serving"] = maybe_serve(db, run.user_id, version) or version.status == "SERVING"
        totals.update(status=version.status, eligible=version.eligible_count, covered=version.embedded_count)
        db.commit()
    if run.error:
        totals["error"] = run.error
    return totals


def run_account(sessions, user_id: int, *, at: datetime | None = None,
                service: EmbeddingService | None = None) -> dict[str, Any]:
    """One indexing pass for one account, one batch at a time (the scheduler runs passes side by side through
    scheduled_jobs.run_live with the same pieces). Never raises for an embedding failure: the summary says what
    happened. `at` is accepted for callers that pass the scheduler's time; claims use the clock."""
    run = prepare(sessions, user_id, service=service)
    if isinstance(run, dict):
        return run
    while (claimed := take(sessions, run)) is not None:
        work(sessions, run, claimed)
    return finish(sessions, run)


# ───────────── retention and status ─────────────

def purge_retired(db: Session, at: datetime) -> int:
    """Delete RETIRED versions past RAG_RETIRED_RETENTION_DAYS, with their entries and vectors."""
    cutoff = at - timedelta(days=max(0, int(get_settings().rag_retired_retention_days or 0)))
    old = db.scalars(select(RagIndexVersion.id).where(RagIndexVersion.status == "RETIRED",
                                                      RagIndexVersion.retired_at < cutoff)).all()
    for version_id in old:
        if vector_store.available(db):
            db.execute(text(f"DELETE FROM {vector_store.TABLE} WHERE index_version_id = :v"), {"v": version_id})
        db.execute(delete(RagIndexEntry).where(RagIndexEntry.index_version_id == version_id))
        db.execute(delete(RagIndexVersion).where(RagIndexVersion.id == version_id))
    db.commit()
    return len(old)


def status(db: Session, user_id: int) -> dict[str, Any]:
    """The account's index: versions with coverage and failures, and whether vectors can be used (metadata only)."""
    from app.services import embeddings

    versions = db.scalars(select(RagIndexVersion).where(RagIndexVersion.user_id == user_id)
                          .order_by(RagIndexVersion.id.desc()).limit(10)).all()
    stored = vector_store.counts(db, user_id) if vector_store.available(db) else {}
    settings = get_settings()
    return {"retrieval_mode": settings.rag_mode, "vector_enabled": settings.rag_vector_enabled,
            "vector_store": vector_store.available(db), "embedding": embeddings.status(db, user_id),
            "versions": [{"id": v.id, "status": v.status, "provider": v.provider, "model": v.model,
                          "underlying_model": v.underlying_model, "dimensions": v.dimensions,
                          "doc_format_version": v.doc_format_version, "eligible": v.eligible_count,
                          "covered": v.embedded_count, "failed": v.failed_count, "vectors": stored.get(v.id, 0),
                          "last_error": v.last_error,
                          "created_at": v.created_at.isoformat() if v.created_at else None,
                          "serving_at": v.serving_at.isoformat() if v.serving_at else None,
                          "retired_at": v.retired_at.isoformat() if v.retired_at else None} for v in versions]}
