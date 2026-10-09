"""Deterministic matching for Job Insights: is an observation about *this* company, role and location?

Third-party data is attached to a job only when the match is good enough, and never by a model:

* company: the canonical domain wins (a different domain rejects); then the canonical name (legal suffixes removed).
  A short or common-word name ("Grab", "Apple", "Meta") with nothing else confirming it — no domain, no matching
  country — is NEEDS_REVIEW and is not used;
* role: titles are normalized into seniority + discipline + core words. The same core is a role match; the same
  discipline at the same level is a *similar* role (kept, lower confidence); anything else is discarded. The mapping
  is recorded on the observation, so a collapsed role is always visible;
* location: city, then country. Compensation from another country is rejected; broad or global data is kept with the
  lowest weight;
* recency: < 12 months HIGH, 12–24 MEDIUM, older LOW. Old data is kept unless an age limit is configured.

The resulting confidence (source × match scope × recency) is the weight every later step uses.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

ACCEPTED, NEEDS_REVIEW, REJECTED = "ACCEPTED", "NEEDS_REVIEW", "REJECTED"

LEGAL_SUFFIXES = {"inc", "incorporated", "ltd", "limited", "llc", "llp", "plc", "pte", "pvt", "private", "corp",
                  "corporation", "co", "company", "gmbh", "ag", "sa", "sas", "bv", "nv", "ab", "oy", "as", "kk",
                  "holdings", "group", "sdn", "bhd", "pty", "srl", "spa"}
# Names that are also common words or that several organizations share: confirmed by a domain or the job's country.
AMBIGUOUS_NAMES = {"apple", "meta", "grab", "stripe", "square", "block", "amazon", "oracle", "shell", "target", "gap",
                   "visa", "delta", "notion", "slack", "zoom", "box", "sea", "bolt", "ring", "nest", "signal", "snap",
                   "uber", "lime", "mercury", "atlas", "apex", "summit", "nova", "orbit", "pilot", "canvas", "wave",
                   "spring", "fusion", "pulse", "vista", "zenith", "unity", "loom", "ramp", "brex", "plaid", "deel"}

SENIORITY = (("INTERN", r"\bintern(ship)?\b"), ("JUNIOR", r"\b(junior|jr|graduate|entry)\b"),
             ("ASSOCIATE", r"\bassociate\b"), ("SENIOR", r"\b(senior|sr)\b"), ("STAFF", r"\bstaff\b"),
             ("PRINCIPAL", r"\bprincipal\b"), ("LEAD", r"\b(lead|tech lead|team lead)\b"),
             ("MANAGER", r"\b(manager|mgr)\b"), ("HEAD", r"\bhead of\b|\bhead\b"), ("DIRECTOR", r"\bdirector\b"),
             ("VP", r"\b(vp|vice president)\b"), ("EXECUTIVE", r"\b(cto|cio|ceo|chief)\b"))
DISCIPLINES = (("ML_AI", r"\b(machine learning|ml|ai|llm|deep learning|genai|artificial intelligence|nlp)\b"),
               ("DATA", r"\b(data|analytics|bi|etl)\b"), ("FRONTEND", r"\b(front ?end|ui|web)\b"),
               ("BACKEND", r"\b(back ?end|server|api)\b"), ("FULLSTACK", r"\bfull ?stack\b"),
               ("MOBILE", r"\b(mobile|ios|android)\b"),
               ("PLATFORM", r"\b(platform|devops|sre|site reliability|infrastructure|cloud)\b"),
               ("SECURITY", r"\b(security|appsec|infosec)\b"), ("QA", r"\b(qa|quality|test|sdet)\b"),
               ("PRODUCT", r"\bproduct\b"), ("DESIGN", r"\b(design|designer|ux)\b"),
               ("SOFTWARE", r"\b(software|engineer|engineering|developer|programmer|swe)\b"))
SYNONYMS = ((r"\bsr\b", "senior"), (r"\bjr\b", "junior"), (r"\bswe\b", "software engineer"), (r"\beng\b", "engineer"),
            (r"\bdev\b", "developer"), (r"\bmgr\b", "manager"), (r"\bback end\b", "backend"),
            (r"\bfront end\b", "frontend"), (r"\bfull stack\b", "fullstack"))
LEVEL_WORDS = {"intern", "internship", "junior", "graduate", "entry", "associate", "senior", "staff", "principal",
               "lead", "head", "of", "ii", "iii", "iv", "i", "1", "2", "3", "4", "level", "team", "tech"}

COUNTRIES = {
    "singapore": "Singapore", "india": "India", "united states": "United States", "usa": "United States",
    "us": "United States", "u.s.": "United States", "america": "United States", "united kingdom": "United Kingdom",
    "uk": "United Kingdom", "england": "United Kingdom", "germany": "Germany", "canada": "Canada",
    "australia": "Australia", "netherlands": "Netherlands", "france": "France", "ireland": "Ireland", "japan": "Japan",
    "uae": "United Arab Emirates", "united arab emirates": "United Arab Emirates", "hong kong": "Hong Kong",
    "indonesia": "Indonesia", "malaysia": "Malaysia", "vietnam": "Vietnam", "philippines": "Philippines",
    "thailand": "Thailand", "spain": "Spain", "sweden": "Sweden", "switzerland": "Switzerland", "poland": "Poland",
    "israel": "Israel", "brazil": "Brazil", "mexico": "Mexico", "new zealand": "New Zealand", "portugal": "Portugal",
    "italy": "Italy", "denmark": "Denmark", "norway": "Norway", "finland": "Finland", "belgium": "Belgium",
    "austria": "Austria", "south korea": "South Korea", "korea": "South Korea", "taiwan": "Taiwan", "china": "China",
}
CITY_STATES = {"Singapore", "Hong Kong"}
US_STATES = {"al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id", "il", "in", "ia", "ks", "ky", "la",
             "me", "md", "ma", "mi", "mn", "ms", "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok",
             "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv", "wi", "wy", "dc"}

# How much a match scope is worth as evidence for this job (multiplied into the observation's confidence).
SCOPE_WEIGHT = {"COMPANY_ROLE_CITY": 1.0, "COMPANY_ROLE_COUNTRY": 0.92, "COMPANY_ROLE": 0.85,
                "COMPANY_SIMILAR_ROLE": 0.75, "COMPANY": 0.8, "MARKET_ROLE_CITY": 0.7, "MARKET_ROLE_COUNTRY": 0.6,
                "MARKET_GLOBAL": 0.35}
RECENCY_WEIGHT = {"HIGH": 1.0, "MEDIUM": 0.8, "LOW": 0.55, "UNKNOWN": 0.7}


def _words(text: str | None) -> str:
    lowered = (text or "").lower().replace("&", " and ")
    lowered = re.sub(r"[^\w\s.+#/-]", " ", lowered).replace("/", " ").replace("-", " ")
    lowered = re.sub(r"\.(?!\w)", " ", lowered)  # "Sr." → "sr"; "node.js" keeps its dot
    for pattern, replacement in SYNONYMS:
        lowered = re.sub(pattern, replacement, lowered)
    return re.sub(r"\s+", " ", lowered).strip()


# ───────────── company ─────────────

def canonical_company(name: str | None) -> str:
    """'Grab Holdings Inc.' → 'grab'; 'Stripe, Inc' → 'stripe'. Lower case, legal suffixes removed."""
    words = [w for w in re.sub(r"[^\w\s&]", " ", (name or "").lower()).split() if w]
    while len(words) > 1 and words[-1] in LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words)


def domain_of(url: str | None) -> str | None:
    """The host of a URL without www., or None. 'https://www.grab.com/sg/about' → 'grab.com'."""
    if not url:
        return None
    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = (parsed.hostname or "").lower()
    host = host.removeprefix("www.")
    return host or None


def same_domain(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return a == b or a.endswith(f".{b}") or b.endswith(f".{a}")


def is_ambiguous(canonical: str) -> bool:
    words = canonical.split()
    return len(words) == 1 and (canonical in AMBIGUOUS_NAMES or len(canonical) <= 4)


# ───────────── role ─────────────

@dataclass(frozen=True)
class Role:
    original: str
    normalized: str
    seniority: str | None
    discipline: str | None
    core: tuple[str, ...]


def normalize_title(title: str | None) -> Role:
    """'Sr. Backend Engineer (Payments)' → normalized 'senior backend engineer', SENIOR, BACKEND."""
    original = (title or "").strip()
    words = _words(re.sub(r"\(.*?\)", " ", original))
    seniority = next((name for name, pattern in SENIORITY if re.search(pattern, words)), None)
    discipline = next((name for name, pattern in DISCIPLINES if re.search(pattern, words)), None)
    core = tuple(w for w in words.split() if w not in LEVEL_WORDS)
    normalized = " ".join(([seniority.lower()] if seniority else []) + list(core))
    return Role(original, normalized, seniority, discipline, core)


def role_match(target: Role, other: Role) -> str:
    """EXACT (same core and level), SIMILAR (same discipline, adjacent or same level) or DIFFERENT."""
    if not other.core:
        return "DIFFERENT"
    a, b = set(target.core), set(other.core)
    overlap = len(a & b) / max(1, len(a | b))
    same_level = target.seniority == other.seniority or not target.seniority or not other.seniority
    if overlap >= 0.75 and same_level:
        return "EXACT"
    if target.discipline and target.discipline == other.discipline and overlap >= 0.3:
        return "SIMILAR"
    if overlap >= 0.6:
        return "SIMILAR"
    return "DIFFERENT"


# ───────────── location ─────────────

@dataclass(frozen=True)
class Place:
    city: str | None
    country: str | None
    remote: bool = False


def parse_location(text: str | None) -> Place:
    """'Singapore' → (Singapore, Singapore); 'Austin, TX' → (Austin, United States); 'Remote - India' → remote, India."""
    raw = (text or "").strip()
    lowered = raw.lower()
    remote = bool(re.search(r"\b(remote|anywhere|work from home|wfh)\b", lowered))
    parts = [p.strip() for p in re.split(r"[,|/·•-]", raw) if p.strip()]
    country = None
    for part in reversed(parts):
        key = part.lower().strip(". ")
        if key in COUNTRIES:
            country = COUNTRIES[key]
            break
        if key in US_STATES and len(parts) > 1:
            country = "United States"
            break
    if country is None:
        country = next((name for key, name in COUNTRIES.items() if len(key) > 3 and re.search(rf"\b{re.escape(key)}\b",
                                                                                             lowered)), None)
    city = None
    for part in parts:
        key = part.lower().strip(". ")
        if key in COUNTRIES or key in US_STATES or re.fullmatch(r"(remote|anywhere|hybrid|on ?site)", key):
            continue
        city = part[:120]
        break
    if country in CITY_STATES:
        city = country
    return Place(city, country, remote)


def location_match(target: Place, other: Place) -> str:
    """CITY, COUNTRY, OTHER (a different country) or UNKNOWN (the observation names no place)."""
    if not other.country and not other.city:
        return "UNKNOWN"
    if target.country and other.country and target.country != other.country:
        return "OTHER"
    if target.city and other.city and target.city.lower() == other.city.lower():
        return "CITY"
    if target.country and other.country == target.country:
        return "COUNTRY"
    return "UNKNOWN"


# ───────────── recency ─────────────

def months_between(earlier: datetime, later: datetime) -> float:
    return (later - earlier).days / 30.44


def recency(observed_at: datetime | None, now: datetime) -> str:
    if observed_at is None:
        return "UNKNOWN"
    months = months_between(observed_at, now)
    return "HIGH" if months < 12 else "MEDIUM" if months < 24 else "LOW"


def parse_date(value: Any) -> datetime | None:
    """A date as providers send it: ISO text, 'Mar 2025', a year, or epoch milliseconds. None when unclear."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if 1990 <= number <= 2100:
            return datetime(int(number), 7, 1)
        if number > 10**11:
            return datetime.fromtimestamp(number / 1000, UTC).replace(tzinfo=None)
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        pass
    for fmt in ("%b %Y", "%B %Y", "%b %d, %Y", "%B %d, %Y", "%Y-%m", "%d %b %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    year = re.fullmatch(r"(19|20)\d{2}", text)
    return datetime(int(text), 7, 1) if year else None


# ───────────── the match itself ─────────────

@dataclass(frozen=True)
class Target:
    """What a job's research is matched against."""

    company: str
    canonical: str
    domain: str | None
    role: Role
    place: Place


@dataclass(frozen=True)
class Match:
    status: str  # ACCEPTED | NEEDS_REVIEW | REJECTED
    scope: str | None
    reason: str
    confidence: float
    recency: str


def target_for(company: str, title: str, location: str, website: str | None) -> Target:
    return Target(company=company or "", canonical=canonical_company(company), domain=domain_of(website),
                  role=normalize_title(title), place=parse_location(location))


def company_check(target: Target, company: str | None, domain: str | None = None,
                  place: Place | None = None) -> tuple[str, str]:
    """Is an observation's employer this job's company? (status, reason)."""
    if domain and target.domain:
        if same_domain(domain, target.domain):
            return ACCEPTED, f"Same domain ({target.domain})"
        return REJECTED, f"Different domain ({domain}, not {target.domain})"
    name = canonical_company(company)
    if not name:
        return ACCEPTED, "No employer named (market data)"
    if name != target.canonical:
        return REJECTED, f"Different company ({company})"
    if is_ambiguous(name):
        if place and target.place.country and place.country == target.place.country:
            return ACCEPTED, f"Name matches and the location is in {place.country}"
        return NEEDS_REVIEW, f"'{company}' is a name several organizations share; nothing confirmed it is this one"
    return ACCEPTED, "Name matches"


def assess(target: Target, *, base_confidence: float, company: str | None = None, domain: str | None = None,
           title: str | None = None, location: str | None = None, observed_at: datetime | None = None,
           now: datetime, compensation: bool = False, company_level: bool = False,
           max_age_months: int = 0) -> Match:
    """Match one observation to the job and weigh it. `company_level` observations (values, reviews) need no role;
    compensation from another country is rejected because pay is not comparable across countries."""
    place = parse_location(location) if location else Place(None, None)
    status, reason = company_check(target, company, domain, place)
    has_company = bool(canonical_company(company)) or bool(domain)
    band = recency(observed_at, now)
    if status != ACCEPTED:
        return Match(status, None, reason, 0.0, band)
    if max_age_months and observed_at is not None and months_between(observed_at, now) > max_age_months:
        return Match(REJECTED, None, f"Older than {max_age_months} months", 0.0, band)
    where = location_match(target.place, place)
    if compensation and where == "OTHER":
        return Match(REJECTED, None, f"Pay in {place.country}, not {target.place.country}", 0.0, band)
    if company_level:
        scope = "COMPANY" if has_company else "MARKET_GLOBAL"
    else:
        role = role_match(target.role, normalize_title(title)) if title else ("EXACT" if has_company else "DIFFERENT")
        if role == "DIFFERENT":
            return Match(REJECTED, None, f"Different role ({title or 'no title'})", 0.0, band)
        if has_company:
            scope = ("COMPANY_SIMILAR_ROLE" if role == "SIMILAR" else
                     "COMPANY_ROLE_CITY" if where == "CITY" else
                     "COMPANY_ROLE_COUNTRY" if where == "COUNTRY" else "COMPANY_ROLE")
        else:
            scope = ("MARKET_ROLE_CITY" if where == "CITY" else "MARKET_ROLE_COUNTRY" if where == "COUNTRY"
                     else "MARKET_GLOBAL")
        if role == "SIMILAR":
            reason = f"{reason}; similar role ({normalize_title(title).normalized} ≈ {target.role.normalized})"
    confidence = max(0.0, min(1.0, base_confidence * SCOPE_WEIGHT[scope] * RECENCY_WEIGHT[band]))
    return Match(ACCEPTED, scope, reason, round(confidence, 3), band)


def cache_key(source_name: str, target: Target, *, company_level: bool = False) -> str:
    """company / role / location / source: two jobs with the same key can share an observation."""
    company = target.domain or target.canonical
    if company_level:
        return f"{source_name}|{company}"[:400]
    place = (target.place.city or target.place.country or "any").lower()
    return f"{source_name}|{company}|{target.role.normalized}|{place}"[:400]
