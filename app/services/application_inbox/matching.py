"""Which tracked application an email is about: deterministic, confidence-scored, and never a guess.

Each application gets a score from the signals the email shows for it, combined as independent evidence
(1 - Π(1 - weight)). The best application is linked automatically only when its score reaches
APPLICATION_EMAIL_AUTO_MATCH_THRESHOLD *and* no other application comes close *and* the email does not point at a
different tracked company. Between the review and auto thresholds the email waits for a person (or for the model to
confirm, app.services.application_inbox.sync); below the review threshold it is not linked at all.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any

from app.services.application_inbox.queries import TrackedApplication, domain_names_company, is_ats
from app.services.gmail import EmailMeta
from app.settings import get_settings

WEIGHTS = {
    "known_thread": 0.95,           # the thread already belongs to this application
    "known_recruiter": 0.90,        # sent by a recruiter recorded on the application
    "application_reference": 0.90,  # the confirmation / requisition id appears in the email
    "company_domain": 0.70,         # sent from the employer's own domain
    "company_named_domain": 0.65,   # sent from a domain that carries the company's name (hr.singtel.com)
    "ats_for_company": 0.60,        # a recruiting system writing as the company ("Docker <no-reply@ashbyhq.com>")
    "application_context": 0.50,    # "your application was sent to Acme", "applying to Acme"
    "company_name_sender": 0.60,    # "Acme Talent <no-reply@greenhouse.io>"
    "company_name_subject": 0.50,
    "company_name_body": 0.35,
    "unique_company": 0.50,         # the only tracked application at the company the email names
    "job_title": 0.45,              # the role's distinctive words appear in the subject or body
    "ats_sender": 0.10,
    "applied_date_proximity": 0.05,  # received within 60 days of applying
}
# The best application must lead the next one by this much to be linked without a person.
AMBIGUITY_MARGIN = 0.05
STRONG = {"known_thread", "known_recruiter", "application_reference"}
NAME_ONLY = {"company_name_sender", "company_name_subject", "company_name_body", "unique_company",
             "application_context", "applied_date_proximity"}
# Job recommendations and alert digests: never about an application already made, whatever companies they list.
JOB_ALERT_SENDERS = re.compile(r"^(job-?alerts?|jobalerts-noreply|jobs-listings|jobs-recommendations?|jobrecommend|"
                               r"recommendations?|alerts?|careers-alerts?|job-?mailer|newsletter)", re.IGNORECASE)
JOB_ALERT_SUBJECT = re.compile(
    r"\bapply now\b|\b(?:is|are) hiring\b|\bnew jobs?\b|\bjobs? (?:for you|matching|similar|you may)|"
    r"\brecommended jobs?\b|\bjob alerts?\b|(?<!applied for )\b\d+\+? (?:new )?\w*\s?jobs?\b|\btop jobs\b|\bjobs? picked",
    re.IGNORECASE)
APPLICATION_WORDS = re.compile(r"\b(application|applying|applied|candidacy|interview|assessment|offer)\b")
NAME_ONLY_CAP = 0.6
TITLE_STOPWORDS = {"senior", "junior", "lead", "staff", "principal", "the", "and", "for", "with", "of", "sr", "jr",
                   "ii", "iii", "iv", "remote", "hybrid", "onsite", "i", "a", "an", "in", "to", "at", "engineer",
                   "manager", "specialist", "associate", "intern"}


@dataclass
class MatchResult:
    application_id: int | None
    score: float
    signals: list[str]
    status: str  # MATCHED | NEEDS_REVIEW | UNMATCHED | IGNORED
    method: str  # DETERMINISTIC | NONE (LLM_ASSISTED and USER are set later)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    reason: str = ""


def _mentions(text: str, key: str) -> bool:
    return bool(key) and re.search(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", text) is not None


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9&+ ]", " ", (text or "").lower()))


def title_words(title: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9+#]+", (title or "").lower()) if len(w) > 1 and w not in TITLE_STOPWORDS}


def is_job_alert(email: EmailMeta) -> bool:
    local = (email.sender_email or "").split("@", 1)[0]
    return bool(JOB_ALERT_SENDERS.match(local) or JOB_ALERT_SUBJECT.search(email.subject or ""))


def _title_matches(title: str, subject: str, text: str) -> bool:
    """The role itself, not a few generic words: the whole title appears, or (in the subject) most of its
    distinctive words do. Alerts that list many roles share words like "AI", "platform" or "lead" with every title."""
    phrase = _norm(re.sub(r"\([^)]*\)", " ", title or "")).strip()
    if len(phrase) >= 8 and (_mentions(subject, phrase) or _mentions(text, phrase)):
        return True
    words = title_words(title)
    return len(words) >= 2 and len(words & set(subject.split())) / len(words) >= 0.8


def _in_application_context(text: str, key: str) -> bool:
    """The company is named next to words about an application ("your application was sent to Acme")."""
    for found in re.finditer(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", text):
        if APPLICATION_WORDS.search(text[max(0, found.start() - 80):found.end() + 60]):
            return True
    return False


def _names_company(app: TrackedApplication, sender_name: str, subject: str, body: str) -> str | None:
    key = app.company_key
    if len(key) < 3:  # "X", "AI": too short to find reliably by name
        return None
    if _mentions(sender_name, key):
        return "company_name_sender"
    if _mentions(subject, key):
        return "company_name_subject"
    if _mentions(body, key):
        return "company_name_body"
    return None


def score(app: TrackedApplication, email: EmailMeta, body: str, apps: list[TrackedApplication]) -> tuple[float, list[str]]:
    sender_name, subject, text = _norm(email.sender_name), _norm(email.subject), _norm(f"{email.snippet} {body}")
    signals: list[str] = []
    if email.thread_id and email.thread_id in app.known_threads:
        signals.append("known_thread")
    if email.sender_email and email.sender_email in app.recruiter_emails:
        signals.append("known_recruiter")
    raw = f"{email.subject}\n{email.snippet}\n{body}"
    if any(re.search(rf"(?<![A-Za-z0-9]){re.escape(ref)}(?![A-Za-z0-9])", raw) for ref in app.references):
        signals.append("application_reference")
    if email.sender_domain and any(email.sender_domain == d or email.sender_domain.endswith("." + d)
                                   for d in app.company_domains):
        signals.append("company_domain")
    elif email.sender_domain and len(app.company_key) >= 3 and domain_names_company(email.sender_domain,
                                                                                     app.company_key, app.company):
        signals.append("company_named_domain")
    ats = is_ats(email.sender_domain)
    if named := _names_company(app, sender_name, subject, text):
        signals.append(named)
        if sum(1 for other in apps if other.company_key == app.company_key) == 1:
            signals.append("unique_company")
        if ats and named == "company_name_sender":
            signals.append("ats_for_company")
        if _in_application_context(f"{subject} . {text}", app.company_key):
            signals.append("application_context")
    if _title_matches(app.title, subject, text):
        signals.append("job_title")
    if ats:
        signals.append("ats_sender")
    if app.applied_at and email.received_at and timedelta(0) <= email.received_at - app.applied_at <= timedelta(days=60):
        signals.append("applied_date_proximity")
    remaining = 1.0
    for signal in signals:
        remaining *= 1 - WEIGHTS[signal]
    # Proximity and an ATS sender are never evidence on their own.
    if set(signals) <= {"ats_sender", "applied_date_proximity"}:
        return 0.0, []
    value = round(1 - remaining, 4)
    # A company name alone — not from the employer, not from a recruiting system, not about the role — is a
    # coincidence (a shop, a newsletter): it stays below review, so its content is neither kept nor sent to a model.
    if set(signals) <= NAME_ONLY:
        value = min(value, NAME_ONLY_CAP)
    return value, signals


def _other_company_named(best: TrackedApplication, email: EmailMeta, apps: list[TrackedApplication],
                         body: str | None = None) -> str | None:
    """Wrong-company protection: the sender or subject names a different tracked company, or the mail comes from
    another tracked company's domain."""
    sender_name, subject = _norm(email.sender_name), _norm(email.subject)
    if body is not None and not (_mentions(sender_name, best.company_key) or _mentions(subject, best.company_key)):
        others = {o.company for o in apps if o.company_key != best.company_key and len(o.company_key) >= 3
                  and _mentions(body, o.company_key)}
        if len(others) >= 2:  # a digest of several applications ("You applied for 5 jobs"): which one is unclear
            return ", ".join(sorted(others)[:3])
    for other in apps:
        if other.company_key == best.company_key or len(other.company_key) < 3:
            continue
        if best.company_key and (other.company_key in best.company_key or best.company_key in other.company_key):
            continue  # "Acme" and "Acme Cloud": one name contains the other, so neither proves anything
        if _mentions(sender_name, other.company_key) or _mentions(subject, other.company_key):
            return other.company
        if email.sender_domain and any(email.sender_domain == d or email.sender_domain.endswith("." + d)
                                       for d in other.company_domains):
            return other.company
    return None


def headers_point_at_application(email: EmailMeta, apps: list[TrackedApplication]) -> bool:
    """Whether an email may be opened: decided from its headers alone (sender, subject, thread), never from the
    body-derived snippet. A recruiting system (ATS, job board, assessment platform) as sender counts: its subject
    and sender name rarely carry the company, the body does."""
    if is_job_alert(email):
        return False
    if is_ats(email.sender_domain):
        return True
    headers_only = replace(email, snippet="")
    return any(score(app, headers_only, "", apps)[1] for app in apps)


def match(email: EmailMeta, body: str, apps: list[TrackedApplication]) -> MatchResult:
    settings = get_settings()
    auto, review = settings.application_email_auto_match_threshold, settings.application_email_review_threshold
    if is_job_alert(email):
        return MatchResult(None, 0.0, [], "IGNORED", "NONE", reason="a job alert, not about an application")
    scored = []
    for app in apps:
        if app.applied_at and email.received_at and email.received_at < app.applied_at - timedelta(days=1):
            continue  # sent before the application existed
        value, signals = score(app, email, body, apps)
        if value > 0:
            scored.append((value, app, signals))
    if not scored:
        return MatchResult(None, 0.0, [], "IGNORED", "NONE", reason="no application signal")
    scored.sort(key=lambda item: (-item[0], item[1].id))
    best_score, best, signals = scored[0]
    candidates = [{"application_id": app.id, "company": app.company, "role": app.title, "score": value,
                   "signals": s} for value, app, s in scored[:5]]
    if best_score < review:
        return MatchResult(None, best_score, signals, "UNMATCHED", "NONE", candidates,
                           reason="below the review threshold")
    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    ambiguous = runner_up >= review and best_score - runner_up < AMBIGUITY_MARGIN
    strong = bool(set(signals) & STRONG)
    other = None if strong else _other_company_named(best, email, apps, _norm(f"{email.snippet} {body}"))
    if best_score >= auto and not ambiguous and other is None:
        return MatchResult(best.id, best_score, signals, "MATCHED", "DETERMINISTIC", candidates)
    reason = (f"also names {other}" if other else "another application scores almost the same" if ambiguous
              else "between the review and auto-match thresholds")
    return MatchResult(best.id, best_score, signals, "NEEDS_REVIEW", "NONE", candidates, reason=reason)
