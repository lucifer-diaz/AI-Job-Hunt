"""Which applications the inbox watches, what is known about each, and the targeted Gmail searches built from it.

Only applications that were actually submitted (`applied_at` set) and are still live are searched. A job that was
only discovered, ranked, tailored or approved has no `applied_at` and is never searched for. The mailbox is never
listed wholesale: every search names a sender, a domain, a company or a reference, and starts at the application date
(or the last sync, minus an overlap).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models import Application, ApplicationEmailMessage
from app.services.contacts import recruiters_of
from app.services.gmail import after_epoch, sync_window_start

# Stages whose applications are no longer watched. Everything else that was submitted is.
UNWATCHED_STAGES = {"closed", "rejected"}
# Recruiting systems that send mail on an employer's behalf. Mail from them is searched only together with the
# company name: an ATS sender alone says nothing about which application it is.
ATS_DOMAINS = {
    "greenhouse.io": "Greenhouse", "greenhouse-mail.io": "Greenhouse", "lever.co": "Lever", "hire.lever.co": "Lever",
    "ashbyhq.com": "Ashby", "myworkday.com": "Workday", "myworkdayjobs.com": "Workday", "workday.com": "Workday",
    "smartrecruiters.com": "SmartRecruiters", "workablemail.com": "Workable", "workable.com": "Workable",
    "linkedin.com": "LinkedIn", "indeed.com": "Indeed", "indeedemail.com": "Indeed", "icims.com": "iCIMS",
    "jobvite.com": "Jobvite", "successfactors.com": "SuccessFactors", "taleo.net": "Taleo", "breezy.hr": "Breezy",
    "recruitee.com": "Recruitee", "teamtailor.com": "Teamtailor", "bamboohr.com": "BambooHR",
    "rippling.com": "Rippling", "grnh.se": "Greenhouse",
    # Job boards that send the application receipts and recruiter messages for postings applied to through them.
    "naukri.com": "Naukri", "bayt.com": "Bayt", "instahyre.com": "Instahyre", "foundit.in": "Foundit",
    "glassdoor.com": "Glassdoor", "ziprecruiter.com": "ZipRecruiter", "wellfound.com": "Wellfound",
    "cutshort.io": "Cutshort", "hirist.tech": "Hirist", "iimjobs.com": "iimjobs",
    "icims.eu": "iCIMS", "avature.net": "Avature", "successfactors.eu": "SuccessFactors",
    # Assessment platforms: they send the invitations and reminders for an employer's tests.
    "hackerrank.com": "HackerRank", "hackerrankforwork.com": "HackerRank", "codility.com": "Codility",
    "codesignal.com": "CodeSignal", "testgorilla.com": "TestGorilla", "hackerearth.com": "HackerEarth",
    "mettl.com": "Mercer Mettl", "imocha.io": "iMocha", "coderbyte.com": "Coderbyte", "karat.com": "Karat",
}
# Job boards and hosting that appear in posting URLs but are not the employer's own domain.
NOT_EMPLOYER_DOMAINS = set(ATS_DOMAINS) | {
    "naukri.com", "glassdoor.com", "bayt.com", "ziprecruiter.com", "monster.com", "wellfound.com", "angel.co",
    "google.com", "googleusercontent.com", "example.com", "example.org", "jobs.lever.co", "boards.greenhouse.io",
    "apply.workable.com", "jobs.ashbyhq.com", "instahyre.com", "foundit.in", "cutshort.io", "hirist.tech",
}
FREE_MAIL = {"gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "outlook.com", "hotmail.com", "live.com",
             "icloud.com", "me.com", "proton.me", "protonmail.com", "aol.com", "zoho.com", "rediffmail.com"}
COMPANY_URL_KEYS = ("company_url", "companyUrl", "company_website", "companyWebsite", "website", "employer_website",
                    "company_link", "companyLink")
REFERENCE_KEYS = ("requisition_id", "requisitionId", "req_id", "reqId", "job_req_id", "jobReqId", "reference",
                  "reference_id", "referenceId")
COMPANY_SUFFIXES = re.compile(r"\b(inc|llc|ltd|limited|corp|corporation|co|gmbh|plc|pvt|private|pte|sa|ag|bv|"
                              r"srl|the)\b\.?", re.IGNORECASE)
QUERY_EXCLUSIONS = "-in:sent -in:drafts -in:chats -in:spam -in:trash"
MAX_QUERY_CHARS = 1400
MAX_THREADS_PER_APPLICATION = 5


def registrable(host: str) -> str:
    """acme.com from careers.acme.com; acme.co.uk from jobs.acme.co.uk (a small heuristic, no public suffix list)."""
    host = (host or "").lower().strip(".").split(":")[0]
    parts = [p for p in host.split(".") if p]
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in {"co", "com", "org", "net", "ac", "gov", "edu"}:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def company_key(name: str) -> str:
    """How a company name is compared: lower case, legal suffixes and punctuation removed."""
    text = COMPANY_SUFFIXES.sub(" ", (name or "").lower())
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9&+ ]", " ", text)).strip()


def is_ats(domain: str) -> bool:
    domain = (domain or "").lower()
    return any(domain == d or domain.endswith("." + d) for d in ATS_DOMAINS)


def domain_names_company(domain: str, key: str, name: str = "") -> bool:
    """Whether a mail or web domain is visibly the company's own: careers.acme.io, hr.singtel.com, sia-partners.com
    for Acme, Singtel and Sia. The registrable name (or one hyphen-separated part of it) must be the company's name,
    or its first word when that word is distinctive (4+ letters)."""
    base = registrable(domain)
    if not base or not key or employer_domain(domain) is None:
        return False
    label = base.split(".")[0]
    words = key.split()
    names = {key.replace(" ", ""), re.sub(r"[^a-z0-9]", "", (name or "").lower())} - {""}
    names |= {words[0]} if len(words[0]) >= 4 else set()
    return label in names or any(part in names for part in label.split("-"))


def employer_domain(domain: str) -> str | None:
    """The domain as an employer's own, or None for ATS, job-board and free-mail domains."""
    base = registrable(domain)
    if not base or base in FREE_MAIL or base in NOT_EMPLOYER_DOMAINS or is_ats(base) or is_ats(domain):
        return None
    return base


@dataclass
class TrackedApplication:
    """What the inbox knows about one submitted application — no candidate data, nothing from its packet."""

    id: int
    company: str
    title: str
    stage: str
    applied_at: datetime | None
    watched: bool
    company_key: str
    recruiter_emails: set[str] = field(default_factory=set)
    recruiter_names: set[str] = field(default_factory=set)
    company_domains: set[str] = field(default_factory=set)
    references: set[str] = field(default_factory=set)
    known_threads: set[str] = field(default_factory=set)

    def facts(self) -> dict[str, str]:
        return {"application_id": self.id, "company": self.company, "role": self.title, "current_stage": self.stage,
                "applied_at": self.applied_at.date().isoformat() if self.applied_at else ""}


def _host(url: str) -> str:
    try:
        return urlparse(url if "://" in (url or "") else f"https://{url}").hostname or ""
    except ValueError:
        return ""


def tracked_applications(db: Session) -> list[TrackedApplication]:
    """Every submitted application of the session's account. `watched` marks the ones Gmail is searched for; the
    others still take part in matching, so an email about a closed application is never filed under another one."""
    apps = db.scalars(select(Application).options(joinedload(Application.job))
                      .where(Application.applied_at.is_not(None)).order_by(Application.id)).unique().all()
    threads: dict[int, list[str]] = {}
    for application_id, thread in db.execute(
            select(ApplicationEmailMessage.application_id, ApplicationEmailMessage.gmail_thread_id)
            .where(ApplicationEmailMessage.application_id.is_not(None),
                   ApplicationEmailMessage.match_status == "MATCHED", ApplicationEmailMessage.gmail_thread_id != "")
            .order_by(ApplicationEmailMessage.received_at.desc())).all():
        bucket = threads.setdefault(application_id, [])
        if thread not in bucket:
            bucket.append(thread)
    out = []
    for app in apps:
        job = app.job
        raw = (job.raw or {}) if job else {}
        recruiters = recruiters_of(app)
        emails = {r["email"].strip().lower() for r in recruiters if "@" in (r.get("email") or "")}
        domains = {d for d in (employer_domain(e.rsplit("@", 1)[1]) for e in emails) if d}
        for key in COMPANY_URL_KEYS:
            if isinstance(raw.get(key), str) and (d := employer_domain(_host(raw[key]))):
                domains.add(d)
        # A posting URL is often a job board or a hosting service (remoteok.com, a careers SaaS): its host counts as
        # the employer's domain only when it visibly carries the company's name.
        host = _host(job.url) if job and job.url else ""
        if host and domain_names_company(host, company_key(job.company), job.company) and (d := employer_domain(host)):
            domains.add(d)
        references = {str(app.confirmation_id).strip()} if app.confirmation_id else set()
        references |= {str(raw[k]).strip() for k in REFERENCE_KEYS if raw.get(k) not in (None, "")}
        stage = app.stage or "applied"
        out.append(TrackedApplication(
            id=app.id, company=(job.company if job else "") or "", title=(job.title if job else "") or "",
            stage=stage, applied_at=app.applied_at, watched=app.status == "applied" and stage not in UNWATCHED_STAGES,
            company_key=company_key(job.company if job else ""), recruiter_emails=emails,
            recruiter_names={r["name"].strip() for r in recruiters if (r.get("name") or "").strip()},
            company_domains=domains,
            references={r for r in references if len(r) >= 4 and re.fullmatch(r"[A-Za-z0-9_\-#/.]+", r)},
            known_threads=set(threads.get(app.id, [])[:MAX_THREADS_PER_APPLICATION])))
    return out


def _quoted(text: str) -> str:
    return '"' + re.sub(r'["\\]', " ", text).strip() + '"'


def _or(terms: list[str]) -> str:
    return terms[0] if len(terms) == 1 else "(" + " OR ".join(terms) + ")"


def application_queries(app: TrackedApplication, last_success: datetime | None, now: datetime) -> list[str]:
    """The Gmail searches for one application, each bounded to mail received after the sync window starts."""
    since = f"after:{after_epoch(sync_window_start(app.applied_at, last_success, now))}"
    terms: list[str] = []
    if app.recruiter_emails:
        terms.append(f"from:{_or(sorted(app.recruiter_emails))}")
    if app.company_domains:
        terms.append(f"from:{_or(sorted(app.company_domains))}")
    company = app.company.strip()
    if len(app.company_key) >= 3:
        terms.append(f"from:{_or(sorted(set(ATS_DOMAINS)))} {_quoted(company)}")
        terms.append(f'{_quoted(company)} (application OR applying OR interview OR position OR role OR candidacy '
                     f'OR offer OR assessment)')
    terms += [_quoted(ref) for ref in sorted(app.references)]
    return [f"{term} {since} {QUERY_EXCLUSIONS}"[:MAX_QUERY_CHARS] for term in terms]


def build_queries(apps: list[TrackedApplication], last_success: datetime | None, now: datetime) -> list[str]:
    """Every search for the watched applications, deduplicated, in a stable order."""
    queries: list[str] = []
    for app in apps:
        if app.watched:
            queries += [q for q in application_queries(app, last_success, now) if q not in queries]
    return queries
