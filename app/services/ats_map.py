"""Which applicant tracking system a posting's application lives on, and what Job Hunt OS knows about applying there.

    detect(url)                    → AtsMatch: the ATS, its tenant (the employer's account on it), the apply domain,
                                     the login domain (per tenant where the ATS has per-employer accounts), whether an
                                     account is needed, the preferred backend and whether guarded submission can ever
                                     be allowed there
    observe(db, job, **facts)      → the account's AtsRoute row for that tenant, created or updated from the posting
                                     and from what a run saw (a login wall, the form's fingerprint, the outcome)

Deterministic: URL patterns only, never a model and never a guess at an employer's site. The host lists of the ATSs
Job Hunt OS already reads descriptions from come from app.services.jd_extractors, so there is one list. A credential
or a saved session belongs to a route's `login_domain`: a Workday login for one employer never reaches another's.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import AtsRoute, Job
from app.services import jd_extractors

NONE, OPTIONAL, ACCOUNT, UNSUPPORTED = "none", "optional", "account", "unsupported"
PLAYWRIGHT, BROWSER_USE, ATS_API = "playwright", "browser_use_webui", "ats_api"


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _hosts(name: str) -> tuple[str, ...]:
    return next(e.hosts for e in jd_extractors.EXTRACTORS if e.name == name)


def _first_path(parsed: Any) -> str:
    return next((p for p in parsed.path.split("/") if p), "")


def _label(parsed: Any) -> str:
    return (parsed.hostname or "").split(".")[0]


def _greenhouse_tenant(parsed: Any) -> str:
    embedded = parse_qs(parsed.query).get("for")
    return (embedded[0] if embedded else _first_path(parsed)).lower()


@dataclass(frozen=True)
class Ats:
    name: str
    label: str
    hosts: tuple[str, ...]
    tenant: Callable[[Any], str]
    auth: str
    backend: str | None
    # Login per employer (Workday, iCIMS, Taleo, SuccessFactors) or per ATS (one account for every employer).
    login_per_tenant: bool = True
    # The employer is part of the host name (acme.wd5.myworkdayjobs.com), so a login kept per host is kept per employer.
    # Where employers share a host (SuccessFactors' career hosts, told apart by `?company=`), a host cannot tell their
    # logins apart: no saved login or session is used there, and a person signs in each time.
    tenant_in_host: bool = True
    # Guarded submission may ever be allowed here: a deterministic form adapter and a confirmation rule exist.
    guarded_capable: bool = False
    adapter: str | None = None
    note: str = ""  # why the route is limited, when it is

    def matches(self, host: str) -> bool:
        return any(host == h or host.endswith("." + h) for h in self.hosts)


ATSES: tuple[Ats, ...] = (
    Ats("greenhouse", "Greenhouse", _hosts("greenhouse"), _greenhouse_tenant, NONE, PLAYWRIGHT,
        guarded_capable=True, adapter="greenhouse"),
    Ats("lever", "Lever", _hosts("lever"), _first_path, NONE, BROWSER_USE),
    # Ashby's hosted form loads through GraphQL POST requests; the application browsers send no POST until a guarded
    # click, so the form never renders there ("Application submission is unavailable"): a person applies.
    Ats("ashby", "Ashby", _hosts("ashby"), _first_path, UNSUPPORTED, None, login_per_tenant=False,
        note="Ashby loads its form through POST requests, which the application browser never sends"),
    Ats("workable", "Workable", _hosts("workable"), lambda p: "" if _first_path(p) == "j" else (
        _first_path(p) if (p.hostname or "").startswith("apply.") else _label(p)), NONE, BROWSER_USE),
    Ats("workday", "Workday", ("myworkdayjobs.com", "myworkdaysite.com"), _label, ACCOUNT, BROWSER_USE),
    Ats("smartrecruiters", "SmartRecruiters", ("smartrecruiters.com",), _first_path, OPTIONAL, BROWSER_USE,
        login_per_tenant=False),
    Ats("icims", "iCIMS", ("icims.com",), lambda p: _label(p).removeprefix("careers-"), ACCOUNT, BROWSER_USE),
    Ats("jobvite", "Jobvite", ("jobvite.com",), _first_path, NONE, BROWSER_USE),
    Ats("bamboohr", "BambooHR", ("bamboohr.com",), _label, NONE, BROWSER_USE),
    Ats("recruitee", "Recruitee", ("recruitee.com",), _label, NONE, BROWSER_USE),
    Ats("teamtailor", "Teamtailor", ("teamtailor.com",), _label, NONE, BROWSER_USE),
    Ats("successfactors", "SAP SuccessFactors", ("successfactors.com", "successfactors.eu"),
        lambda p: (parse_qs(p.query).get("company") or [_label(p)])[0].lower(), ACCOUNT, BROWSER_USE,
        tenant_in_host=False),
    Ats("taleo", "Oracle Taleo", ("taleo.net",), _label, ACCOUNT, BROWSER_USE),
    # In-board applications behind the board's own login: Job Hunt OS never automates them.
    Ats("linkedin", "LinkedIn Easy Apply", _hosts("linkedin"), lambda p: "", UNSUPPORTED, None, login_per_tenant=False,
        note="LinkedIn applications happen inside the board, behind its own login"),
    Ats("indeed", "Indeed Apply", _hosts("indeed"), lambda p: "", UNSUPPORTED, None, login_per_tenant=False,
        note="Indeed applications happen inside the board, behind its own login"),
)
GENERIC = Ats("generic", "Employer site", (), lambda p: (p.hostname or "").lower(), NONE, BROWSER_USE)


@dataclass(frozen=True)
class AtsMatch:
    ats: str
    label: str
    tenant: str
    apply_domain: str
    login_domain: str | None
    auth_required: str
    backend: str | None
    guarded_capable: bool
    adapter: str | None
    note: str = ""

    @property
    def shared_login_host(self) -> bool:
        """Employers share this host (its logins cannot be kept apart per employer): nothing is saved for it."""
        return self.auth_required in (ACCOUNT, OPTIONAL) and self.login_domain is None

    @property
    def route(self) -> str:
        """The route key settings and the allowlist use: "greenhouse:acme", "generic:careers.example.com"."""
        return f"{self.ats}:{self.tenant}" if self.tenant else self.ats

    def allowed_by(self, allowlist: frozenset[str]) -> bool:
        """A route is allowlisted by its exact key or by its ATS as a whole ("greenhouse")."""
        return self.guarded_capable and (self.route in allowlist or self.ats in allowlist)


def ats_named(name: str) -> Ats:
    return next((a for a in ATSES if a.name == name), GENERIC)


def detect(url: str | None) -> AtsMatch | None:
    """The ATS a URL belongs to, or None for something that is not a web address."""
    parsed = urlparse((url or "").strip())
    host = (parsed.hostname or "").lower().strip(".")
    if parsed.scheme not in ("http", "https") or not host:
        return None
    ats = next((a for a in ATSES if a.matches(host)), GENERIC)
    tenant = re.sub(r"[^a-z0-9._-]", "", (ats.tenant(parsed) or "").lower())[:200]
    login = None
    if ats.auth == UNSUPPORTED:
        return AtsMatch(ats=ats.name, label=ats.label, tenant=tenant, apply_domain=host, login_domain=None,
                        auth_required=UNSUPPORTED, backend=None, guarded_capable=False, adapter=None, note=ats.note)
    if ats.auth in (ACCOUNT, OPTIONAL) and (ats.tenant_in_host or not ats.login_per_tenant):
        login = host if ats.login_per_tenant else next((h for h in ats.hosts if host.endswith(h)), host)
    return AtsMatch(ats=ats.name, label=ats.label, tenant=tenant, apply_domain=host, login_domain=login,
                    auth_required=ats.auth, backend=ats.backend, guarded_capable=ats.guarded_capable,
                    adapter=ats.adapter)


def for_job(job: Job) -> AtsMatch | None:
    from app.services.apply_queue import apply_url

    return detect(apply_url(job))


def observe(db: Session, job: Job, **facts: Any) -> AtsRoute | None:
    """Create or update the account's route for this job's ATS tenant with what is known now (`facts`: backend,
    form_fingerprint, last_outcome, needs_login). Flushes in a savepoint; never raises; the caller commits."""
    match = for_job(job)
    if match is None:
        return None
    try:
        with db.begin_nested():
            route = db.scalar(select(AtsRoute).where(AtsRoute.user_id == job.user_id, AtsRoute.ats == match.ats,
                                                     AtsRoute.tenant == match.tenant))
            if route is None:
                route = AtsRoute(user_id=job.user_id, ats=match.ats, tenant=match.tenant,
                                 apply_domain=match.apply_domain, auth_required=match.auth_required,
                                 created_at=utcnow())
                db.add(route)
            route.apply_domain, route.login_domain = match.apply_domain, match.login_domain
            route.auth_required = match.auth_required
            route.backend = facts.pop("backend", None) or route.backend or match.backend
            route.source_board = job.source_board or route.source_board
            route.company = (job.company or route.company or "")[:255] or None
            route.last_job_id = job.id
            for name in ("form_fingerprint", "last_outcome", "needs_login"):
                if name in facts and facts[name] is not None:
                    setattr(route, name, facts[name])
            if facts.get("checked"):
                route.last_checked_at = utcnow()
            db.flush()
        return route
    except IntegrityError:  # another run created the same route at the same moment
        return db.scalar(select(AtsRoute).where(AtsRoute.user_id == job.user_id, AtsRoute.ats == match.ats,
                                                AtsRoute.tenant == match.tenant))
    except Exception:  # noqa: BLE001  the map is a convenience; a run never fails because of it
        return None


def refresh(db: Session, user_id: int, limit: int = 300) -> int:
    """Fill the map from the account's approved and applied jobs (the routes they will be submitted through). Does
    not commit."""
    jobs = db.scalars(select(Job).where(Job.user_id == user_id, Job.status.in_(("approved", "applied")))
                      .order_by(Job.id.desc()).limit(limit)).all()
    return sum(1 for job in jobs if observe(db, job) is not None)


def view(db: Session, route: AtsRoute) -> dict[str, Any]:
    """A route for Settings → Application routes: what it is, whether a login is saved, and whether guarded
    submission could run there (capable and allowlisted on this server)."""
    from app.models import AtsCredential, AtsSession
    from app.settings import get_settings

    ats = ats_named(route.ats)
    match = AtsMatch(ats=route.ats, label=ats.label, tenant=route.tenant, apply_domain=route.apply_domain,
                     login_domain=route.login_domain, auth_required=route.auth_required, backend=route.backend,
                     guarded_capable=ats.guarded_capable, adapter=ats.adapter)
    domain = route.login_domain
    credential = db.scalar(select(AtsCredential.status).where(AtsCredential.user_id == route.user_id,
                                                              AtsCredential.domain == domain)) if domain else None
    session = db.scalar(select(AtsSession).where(AtsSession.user_id == route.user_id, AtsSession.domain == domain,
                                                 AtsSession.revoked_at.is_(None))) if domain else None
    return {"id": route.id, "route": match.route, "ats": route.ats, "label": ats.label, "tenant": route.tenant,
            "company": route.company, "source_board": route.source_board, "apply_domain": route.apply_domain,
            "login_domain": domain, "auth_required": route.auth_required, "backend": route.backend,
            "needs_login": route.needs_login, "last_outcome": route.last_outcome,
            "last_checked_at": route.last_checked_at.isoformat() if route.last_checked_at else None,
            "has_credential": credential == "ACTIVE",
            "has_session": session is not None and session.expires_at > utcnow(),
            "guarded_capable": ats.guarded_capable, "note": ats.note or None,
            "guarded_allowed": match.allowed_by(get_settings().guarded_routes)}
