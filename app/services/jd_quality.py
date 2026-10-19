"""Is this a job description? Deterministic checks, no model.

Three questions, asked in three places:

* `description_problem(text)` — is the description a posting arrived with usable, or should it be recovered from the
  posting URL? Empty, placeholder ("View job", "Apply here"), truncated ("…see more"), navigation/footer text, or too
  short to carry a posting are unusable. Length is never the only test: a short posting with real structure passes.
* `classify_page(...)` — what a fetched page is when it is not a posting: removed (404, "no longer accepting
  applications"), a login wall, a CAPTCHA or anti-bot challenge, or a generic error page. Blocked pages are reported,
  never bypassed.
* `validate_recovered(...)` — may recovered text be saved? It must be long enough, mostly useful text, carry job
  signals, and must not be a login, CAPTCHA, error or unrelated page.

`content_hash` is the normalized-text hash used to notice that recovered text is the same posting already stored.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from app.settings import get_settings

# Problems with a description as it arrived (Job.description_quality).
USABLE = "USABLE"
EMPTY, PLACEHOLDER, TRUNCATED, BOILERPLATE, TOO_SHORT = "EMPTY", "PLACEHOLDER", "TRUNCATED", "BOILERPLATE", "TOO_SHORT"
QUALITY_LABELS = {USABLE: "Usable", EMPTY: "Missing", PLACEHOLDER: "Placeholder only", TRUNCATED: "Truncated",
                  BOILERPLATE: "Site text, not a posting", TOO_SHORT: "Too short"}

# What a fetched page turned out to be, when it is not a posting (JobDescriptionFetchTask.result_code).
JOB_REMOVED, JOB_CLOSED = "JOB_REMOVED", "JOB_CLOSED"
LOGIN_REQUIRED, CAPTCHA_OR_ANTI_BOT, ACCESS_DENIED = "LOGIN_REQUIRED", "CAPTCHA_OR_ANTI_BOT", "ACCESS_DENIED"
ERROR_PAGE, UNRELATED_PAGE = "ERROR_PAGE", "UNRELATED_PAGE"

PLACEHOLDER_PHRASES = (
    "view job", "view this job", "view the job", "view posting", "apply here", "apply now", "apply today",
    "click to apply", "click here to apply", "click here", "see job posting", "see posting", "see the job posting",
    "see job description", "see full job description", "job description", "full job description", "no description",
    "description not available", "no description available", "details on website", "apply on company website",
    "apply on our careers site", "apply on our website", "more details", "read more", "see more", "show more",
    "learn more", "visit our website", "n/a", "na", "tbd", "none", "-")
TRUNCATION_TAILS = ("…", "...", "see more", "show more", "read more", "[truncated]", "(truncated)", "more »",
                    "…see more", "... see more", "…show more", "click to read more")
# Words of the site around a posting, not of the posting.
BOILERPLATE_WORDS = ("privacy policy", "cookie", "cookies", "terms of use", "terms of service", "all rights reserved",
                     "sign in", "log in", "sign up", "join now", "forgot password", "skip to main content",
                     "accessibility", "sitemap", "contact us", "follow us", "subscribe", "download the app",
                     "similar jobs", "people also viewed", "recommended jobs", "jobs you may be interested in",
                     "copyright", "©", "careers home", "back to jobs", "search jobs", "job alerts")
# What real postings say. Counted as distinct signals.
JOB_SIGNALS = (
    "responsibilit", "requirement", "qualification", "experience", "skills", "you will", "you'll", "what you",
    "about the role", "about the job", "about you", "who you are", "the role", "we are looking for",
    "we're looking for", "looking for a", "years of", "must have", "nice to have", "preferred", "degree",
    "benefits", "salary", "compensation", "team", "collaborate", "design", "build", "develop", "manage", "lead",
    "ownership", "stakeholder", "job type", "employment type", "full-time", "full time", "part-time", "contract",
    "remote", "hybrid", "on-site", "onsite", "location", "apply", "role")
STRONG_SIGNALS = ("responsibilit", "requirement", "qualification", "what you'll do", "what you will do",
                  "about the role", "about the job", "about you", "who you are", "must have", "nice to have",
                  "years of experience", "we are looking for", "we're looking for", "preferred qualifications",
                  "minimum qualifications", "basic qualifications", "key responsibilities", "your role")

LOGIN_MARKERS = ("sign in to continue", "sign in to view", "log in to continue", "log in to view", "login to view",
                 "please sign in", "please log in", "you must be logged in", "join to view", "sign in to see",
                 "sign in to apply and view", "authwall", "create an account to view", "log in or sign up",
                 "sign in or join", "session expired")
CAPTCHA_MARKERS = ("captcha", "are you a robot", "are you human", "verify you are human", "verify you are a human",
                   "checking your browser", "just a moment...", "attention required! | cloudflare",
                   "cf-browser-verification", "cf_chl_", "challenge-platform", "ddos protection by",
                   "press & hold", "press and hold", "px-captcha", "unusual traffic", "automated queries",
                   "bot detection", "access to this page has been denied", "request unsuccessful. incapsula",
                   "perimeterx", "distil networks", "pardon our interruption")
CLOSED_MARKERS = ("no longer accepting applications", "this job is no longer available", "job is no longer available",
                  "this job has expired", "job has expired", "this position has been filled", "position has been filled",
                  "this job posting has been removed", "job posting is no longer", "this posting is closed",
                  "this job is closed", "no longer open", "the job you are looking for is no longer",
                  "this job has been closed", "this role has been filled", "job not found", "posting not found",
                  "position is no longer available", "vacancy has been closed", "this requisition is closed")
# Phrases that close a posting even on a page that still shows its description (the listing check only): a banner
# the board puts on the posting itself, never a phrase a description would use.
CLOSED_BANNER_MARKERS = ("no longer accepting applications", "this job is no longer available",
                         "this job has expired", "this posting is closed", "this job is closed",
                         "this position has been filled", "this job posting has been removed")
ERROR_MARKERS = ("page not found", "404 not found", "error 404", "something went wrong", "an error occurred",
                 "internal server error", "service unavailable", "bad gateway", "please enable javascript",
                 "you need to enable javascript", "this site requires javascript", "we can't find that page",
                 "we couldn't find", "oops!")


def _lower(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


lower_text = _lower


def content_hash(text: str | None) -> str | None:
    """A hash of the posting's words: case, spacing, bullets and markup do not change it. None for no text."""
    from app.services.job_description import normalize_description

    words = re.findall(r"[\w$€£₹%+#.-]+", normalize_description(text or "").lower())
    return hashlib.sha256(" ".join(words).encode("utf-8")).hexdigest() if words else None


def signals(text: str | None) -> tuple[int, int]:
    """(distinct job signals, distinct strong signals) in the text."""
    lowered = _lower(text)
    return (sum(1 for word in JOB_SIGNALS if word in lowered), sum(1 for word in STRONG_SIGNALS if word in lowered))


def boilerplate_ratio(text: str | None) -> float:
    """The share of non-empty lines that are site furniture: boilerplate phrases, or very short link-like lines."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return 1.0
    noisy = 0
    for line in lines:
        lowered = line.lower()
        words = len(lowered.split())
        if any(word in lowered for word in BOILERPLATE_WORDS) and words <= 14:
            noisy += 1
        elif words <= 2 and not lowered.startswith("- ") and not lowered.endswith(":") and len(lowered) < 20:
            noisy += 1  # "Home", "Jobs", "Apply", menu items
    return noisy / len(lines)


def _minimum(minimum: int | None) -> int:
    return max(1, get_settings().jd_description_min_characters if minimum is None else minimum)


def _is_placeholder(text: str) -> bool:
    lowered = _lower(text).strip(" .!:-–—>»|")
    if lowered in PLACEHOLDER_PHRASES:
        return True
    # "Apply here: https://…" and other one-liners made only of a placeholder and a link.
    without_links = re.sub(r"https?://\S+", "", lowered).strip(" .!:-–—>»|")
    return len(without_links) < 60 and any(without_links == p or without_links.startswith(p + " ")
                                           for p in PLACEHOLDER_PHRASES if len(p) > 3)


def _is_truncated(text: str) -> bool:
    tail = _lower(text)[-40:]
    return any(tail.endswith(marker) for marker in TRUNCATION_TAILS)


def description_problem(text: str | None, *, minimum: int | None = None) -> str | None:
    """Why a description is not usable, or None when it is. Deterministic, and never by length alone."""
    body = (text or "").strip()
    if not body:
        return EMPTY
    if _is_placeholder(body):
        return PLACEHOLDER
    floor = _minimum(minimum)
    length = len(body)
    found, strong = signals(body)
    # A posting cut off by the board ("…see more") is truncated even when it is fairly long.
    if _is_truncated(body) and length < floor * 3:
        return TRUNCATED
    if boilerplate_ratio(body) >= 0.6 and strong == 0:
        return BOILERPLATE
    if length < floor:
        # Short postings exist: one with real structure is usable, a sentence or two is not.
        if length >= floor * 0.6 and strong >= 2 and found >= 6:
            return None
        return TOO_SHORT
    if found < 2 and strong == 0 and length < floor * 2:
        return BOILERPLATE  # long enough, but it does not read like a posting at all
    return None


def quality_of(text: str | None, *, minimum: int | None = None) -> str:
    return description_problem(text, minimum=minimum) or USABLE


def is_job_description_usable(job: Any, *, minimum: int | None = None) -> bool:
    """True when the job's description is good enough to parse and rank; False when it should be recovered.

    Text the user typed or pasted is always respected: recovery never overrides a person.
    """
    if getattr(job, "description_source", None) == "USER" and (getattr(job, "description", "") or "").strip():
        return True
    return description_problem(getattr(job, "description", None), minimum=minimum) is None


# ───────────── pages that are not postings ─────────────

def classify_page(*, status: int | None, text: str | None, title: str | None = None,
                  has_password_field: bool = False) -> str | None:
    """What a fetched page is when it is not something to extract from, or None to go ahead and extract.

    Called before extraction for statuses and challenges, and again when extraction finds no posting (for closed
    postings and error pages, which can share a page with ordinary content).
    """
    lowered = f"{_lower(title)} {_lower(text)[:20000]}"
    if status in (404, 410):
        return JOB_REMOVED
    if any(marker in lowered for marker in CAPTCHA_MARKERS) and len(_lower(text)) < 6000:
        return CAPTCHA_OR_ANTI_BOT
    if status == 429:
        return None  # rate limited: a temporary condition, retried later rather than classified
    login = any(marker in lowered for marker in LOGIN_MARKERS)
    if status in (401, 407) or (login and (has_password_field or status == 403 or len(_lower(text)) < 4000)):
        return LOGIN_REQUIRED
    if status == 403:
        return ACCESS_DENIED
    return None


def closed_or_error(text: str | None, title: str | None = None) -> str | None:
    """JOB_CLOSED or ERROR_PAGE when a page with no posting on it says so."""
    lowered = f"{_lower(title)} {_lower(text)[:20000]}"
    if any(marker in lowered for marker in CLOSED_MARKERS):
        return JOB_CLOSED
    if any(marker in lowered for marker in ERROR_MARKERS):
        return ERROR_PAGE
    return None


# ───────────── validating recovered text ─────────────

@dataclass
class Validation:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    length: int = 0
    signals: int = 0
    strong_signals: int = 0
    useful_ratio: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "reasons": self.reasons, "length": self.length, "signals": self.signals,
                "strong_signals": self.strong_signals, "useful_ratio": round(self.useful_ratio, 3)}


GENERIC_PATHS = {"", "/", "/jobs", "/careers", "/career", "/jobs/search", "/search", "/en", "/en-us", "/home",
                 "/job-search", "/opportunities", "/openings", "/positions", "/join-us", "/login", "/signin"}
_STOPWORDS = {"and", "the", "for", "with", "of", "to", "in", "a", "an", "at", "on", "senior", "junior", "sr", "jr",
              "lead", "staff", "principal", "i", "ii", "iii", "iv", "remote", "hybrid", "-", "–", "&", "/"}


def unrelated_redirect(source_url: str | None, final_url: str | None, text: str, title: str | None) -> bool:
    """True when a redirect landed on a generic page (a careers home, a search, a login) rather than the posting."""
    if not source_url or not final_url or source_url.rstrip("/") == final_url.rstrip("/"):
        return False
    source, final = urlparse(source_url), urlparse(final_url)
    source_path, final_path = source.path.rstrip("/").lower(), final.path.rstrip("/").lower()
    if final_path in GENERIC_PATHS and source_path not in GENERIC_PATHS:
        return True
    if source.hostname != final.hostname and title:
        # Aggregator → employer ATS is normal; landing somewhere that never names the role is not.
        words = [w for w in re.findall(r"[a-z0-9+#]{3,}", title.lower()) if w not in _STOPWORDS]
        return bool(words) and not any(word in _lower(text) for word in words)
    return False


def validate_recovered(text: str | None, *, job_title: str | None = None, source_url: str | None = None,
                       final_url: str | None = None, minimum: int | None = None) -> Validation:
    """May this text be saved as the job's description? Every check is deterministic."""
    body = (text or "").strip()
    found, strong = signals(body)
    useful = 1.0 - boilerplate_ratio(body)
    result = Validation(ok=False, length=len(body), signals=found, strong_signals=strong, useful_ratio=useful)
    floor = _minimum(minimum)
    lowered = _lower(body)
    if not body:
        result.reasons.append("EMPTY")
        return result
    if len(body) < floor and not (len(body) >= floor * 0.6 and strong >= 2 and found >= 6):
        result.reasons.append("TOO_SHORT")
    if useful < 0.5:
        result.reasons.append("MOSTLY_BOILERPLATE")
    if found < 3 and strong == 0:
        result.reasons.append("NO_JOB_CONTENT")
    if len(lowered) < 4000 and any(marker in lowered for marker in LOGIN_MARKERS):
        result.reasons.append(LOGIN_REQUIRED)
    if len(lowered) < 6000 and any(marker in lowered for marker in CAPTCHA_MARKERS):
        result.reasons.append(CAPTCHA_OR_ANTI_BOT)
    if strong == 0 and any(marker in lowered for marker in ERROR_MARKERS):
        result.reasons.append(ERROR_PAGE)
    if unrelated_redirect(source_url, final_url, body, job_title):
        result.reasons.append(UNRELATED_PAGE)
    result.ok = not result.reasons
    return result


def _words(text: str | None) -> set[str]:
    return set(re.findall(r"[a-z0-9+#]{3,}", (text or "").lower()))


def better_than(candidate: str, current: str | None, *, minimum: int | None = None) -> bool:
    """Whether recovered text should replace the current description.

    An unusable description is always replaced. A usable one only by a clearly fuller version of the same posting:
    at least half as long again, as strong in posting signals, and containing (nearly) every word of the current
    text. A different posting, or a shorter or weaker one, never replaces it."""
    if description_problem(current, minimum=minimum) is not None:
        return True
    existing = _words(current)
    covered = len(existing & _words(candidate)) / len(existing) if existing else 1.0
    return (len(candidate) >= len((current or "").strip()) * 1.5 and signals(candidate)[1] >= signals(current)[1]
            and covered >= 0.85)
