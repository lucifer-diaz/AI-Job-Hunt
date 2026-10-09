"""Job Insights sources that need no third-party research service: the posting, the recruiter, the company's own site.

Source priority (highest first) — the order facts are trusted in, and the order the screen lists them:
  A. the employer's posting (salary, benefits, remote policy, the company's own words in the posting)
  B. what the person entered from the recruiter (budget, interview process, questions asked)
  C. the company's official website (mission, values, products, careers statements)
  D–E. company-specific and market research through Apify (app.services.job_insights.apify)
  F. public web search — no provider is configured, so it is always NOT_CONFIGURED
  G. preparation a model writes from the above (app.services.job_insights.synthesis), always labelled GENERATED

The official site is read over plain HTTP through the instrumented client, one redirect at a time with every hop
checked (app.services.url_safety), and only from a website the posting named or the person entered: a domain is never
guessed. Pages behind a login, CAPTCHA or anti-bot check are recorded as BLOCKED and never bypassed. Only short
extracts are kept (a value's name, a mission sentence, a capped excerpt), never a page.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from app.services import jd_quality
from app.services.job_insights import matching
from app.services.job_insights.base import (
    BLOCKED,
    FAILED,
    NO_DATA,
    NOT_CONFIGURED,
    OK,
    ObservationIn,
    Snapshot,
    SourceResult,
)
from app.settings import get_settings

MAX_PAGES = 5
MAX_HTML = 2_000_000
MAX_REDIRECTS = 6
EXTRACT_CHARS = 1200
PAGE_HINTS = ("about", "mission", "values", "culture", "careers", "who-we-are", "principles", "life-at", "our-story",
              "company", "benefits")
FALLBACK_PATHS = ("/about", "/about-us", "/company", "/careers", "/values", "/culture")
VALUE_HEADINGS = re.compile(r"\b(our values|core values|values|principles|what we believe|how we work|our culture "
                            r"code|leadership principles)\b", re.IGNORECASE)
MISSION_WORDS = re.compile(r"\b(our mission|mission is|our vision|vision is|we exist to|our purpose)\b", re.IGNORECASE)
BENEFIT_HEADINGS = re.compile(r"\b(benefits|perks|what we offer)\b", re.IGNORECASE)
REMOVED_TAGS = ("script", "style", "noscript", "svg", "nav", "footer", "header", "form", "iframe", "template")


def _salary_data(found: dict[str, Any], text: str | None = None) -> dict[str, Any]:
    stated = text or found.get("text") or ""
    basis = "BASE" if re.search(r"\bbase\b", stated, re.IGNORECASE) else "UNKNOWN"
    return {"low": found.get("min"), "high": found.get("max"), "currency": found.get("currency"),
            "period": found.get("period"), "basis": basis, "text": stated[:200] or None,
            "field": found.get("field")}


class PostingSource:
    """A. What the employer's own posting says: its salary first of all, and the company's own words."""

    name, label, source_type, company_level = "job_posting", "Job posting", "EMPLOYER", False

    def configured(self) -> str | None:
        return None

    def fetch(self, snapshot: Snapshot) -> SourceResult:
        out: list[ObservationIn] = []
        common = {"recency": "HIGH", "match_scope": "POSTING", "match_reason": "This job's own posting"}
        board = snapshot.board_salary or {}
        estimate = "estimate" in str(board.get("salary_source") or "").lower()
        if snapshot.posting_salary and snapshot.posting_salary.get("min") is not None:
            out.append(ObservationIn("COMPENSATION", "EMPLOYER", _salary_data(snapshot.posting_salary), 0.98,
                                     title="Salary stated in the posting", **common))
        elif (board.get("salary_min") or board.get("salary_max")) and not estimate:
            found = {"min": board.get("salary_min") or board.get("salary_max"), "max": board.get("salary_max"),
                     "currency": board.get("salary_currency"), "period": board.get("salary_interval"),
                     "field": "board"}
            out.append(ObservationIn("COMPENSATION", "EMPLOYER", _salary_data(found), 0.95,
                                     title="Salary the job board reported for this posting", **common))
        else:
            parsed = (snapshot.structured or {}).get("compensation") or {}
            if parsed.get("source") == "job_description" and (parsed.get("min") or parsed.get("max")):
                found = {"min": parsed.get("min") or parsed.get("max"), "max": parsed.get("max"),
                         "currency": parsed.get("currency"), "period": parsed.get("period"), "field": "description"}
                out.append(ObservationIn("COMPENSATION", "JOB_DESCRIPTION",
                                         _salary_data(found, parsed.get("source_text")), 0.9,
                                         title="Salary quoted in the job description", **common))
        if estimate and (board.get("salary_min") or board.get("salary_max")):
            # The board's own estimate (Glassdoor shows one beside postings): market data, never the employer's.
            found = {"min": board.get("salary_min") or board.get("salary_max"), "max": board.get("salary_max"),
                     "currency": board.get("salary_currency"), "period": board.get("salary_interval"),
                     "field": "board_estimate"}
            out.append(ObservationIn("COMPENSATION", "GLASSDOOR", {**_salary_data(found), "basis": "BASE"}, 0.6,
                                     title="Salary estimate shown with the posting (not stated by the employer)",
                                     provider="JOB_BOARD", recency="HIGH", match_scope="COMPANY_ROLE",
                                     match_reason="The job board's estimate for this posting"))
        structured = snapshot.structured or {}
        for phrase in (structured.get("company_context") or [])[:8]:
            if isinstance(phrase, str) and phrase.strip():
                out.append(ObservationIn("COMPANY", "JOB_DESCRIPTION", {"field": "posting_company_context",
                                                                          "text": phrase.strip()[:400]}, 0.9,
                                         title="Company description in the posting", **common))
        identity = structured.get("job_identity") or {}
        if identity.get("remote_type"):
            out.append(ObservationIn("COMPANY", "JOB_DESCRIPTION", {"field": "remote_policy",
                                                                      "text": str(identity["remote_type"])[:100]}, 0.9,
                                     title="Work arrangement stated in the posting", **common))
        where = structured.get("location_requirements") or {}
        if where.get("visa_sponsorship") is not None:
            out.append(ObservationIn("COMPANY", "JOB_DESCRIPTION", {"field": "visa_sponsorship",
                                                                      "text": str(where["visa_sponsorship"])[:100]},
                                     0.9, title="Visa sponsorship stated in the posting", **common))
        if not out:
            return SourceResult(self.name, NO_DATA, detail="The posting states no salary or company details")
        has_salary = any(o.category == "COMPENSATION" and o.source_type != "GLASSDOOR" for o in out)
        return SourceResult(self.name, OK, out, detail=None if has_salary else "The posting states no salary")


def _lines(value: Any) -> list[str]:
    if isinstance(value, list):
        items = value
    else:
        items = re.split(r"[\n;]+", str(value or ""))
    return [str(item).strip(" -•*\t")[:300] for item in items if str(item).strip(" -•*\t")]


class RecruiterSource:
    """B. What the person was told by the recruiter. It outranks every estimate and research never overwrites it."""

    name, label, source_type, company_level = "recruiter", "Recruiter notes", "RECRUITER", False

    def configured(self) -> str | None:
        return None

    def fetch(self, snapshot: Snapshot) -> SourceResult:
        notes = snapshot.recruiter or {}
        out: list[ObservationIn] = []
        common = {"recency": "HIGH", "match_scope": "RECRUITER", "match_reason": "Entered by you from the recruiter"}
        low, high = notes.get("budget_low"), notes.get("budget_high")
        if low or high:
            out.append(ObservationIn("COMPENSATION", "RECRUITER", {
                "low": low or high, "high": high, "currency": notes.get("currency"), "period": notes.get("period"),
                "basis": notes.get("basis") or "UNKNOWN", "text": "Budget stated by the recruiter"}, 0.95,
                title=f"Budget from {notes.get('recruiter_name') or 'the recruiter'}", **common))
        if notes.get("interview_process"):
            out.append(ObservationIn("INTERVIEW_PROCESS", "RECRUITER", {
                "text": str(notes["interview_process"])[:1000], "stages": _lines(notes["interview_process"])[:10]},
                0.95, title="Interview process described by the recruiter", **common))
        for question in _lines(notes.get("recruiter_questions"))[:20]:
            out.append(ObservationIn("INTERVIEW_QUESTION", "RECRUITER", {"question": question, "stage": None}, 0.9,
                                     title="Question the recruiter mentioned", **common))
        if notes.get("comments"):
            out.append(ObservationIn("COMPANY", "RECRUITER", {"field": "recruiter_comments",
                                                                "text": str(notes["comments"])[:1000]}, 0.9,
                                     title="Recruiter comments", **common))
        if not out:
            return SourceResult(self.name, NO_DATA, detail="No recruiter notes yet")
        return SourceResult(self.name, OK, out)


# ───────────── the official company website ─────────────

class PageBlocked(Exception):
    pass


def _get(client: httpx.Client, url: str, *, allow_private: bool) -> tuple[int, str, str]:
    """GET one page, following redirects one hop at a time, each hop checked. (status, final url, html)."""
    from app.services.url_safety import check_public_url

    current, seen = url, {url}
    for _ in range(MAX_REDIRECTS + 1):
        check_public_url(current, allow_private=allow_private)
        response = client.get(current)
        if response.is_redirect and response.headers.get("location"):
            target = urljoin(current, response.headers["location"])
            if target in seen:
                raise httpx.TooManyRedirects("redirect loop", request=response.request)
            seen.add(target)
            current = target
            continue
        content_type = response.headers.get("content-type", "").lower()
        if content_type and "html" not in content_type:
            return response.status_code, current, ""
        return response.status_code, current, response.text[:MAX_HTML]
    raise httpx.TooManyRedirects("too many redirects")


def _clean_soup(html: str) -> BeautifulSoup:
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(list(REMOVED_TAGS)):
        tag.decompose()
    return soup


def _text(node: Any) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True) if node is not None else "").strip()


def _after_heading(heading: Any, limit: int) -> list[str]:
    """Short items that follow a heading: list items, or sub-headings, up to the next heading of its level."""
    level = int(heading.name[1]) if heading.name and heading.name[1:].isdigit() else 2
    items: list[str] = []
    for node in heading.find_all_next(["h1", "h2", "h3", "h4", "h5", "li", "dt", "strong"]):
        if node.name and node.name[0] == "h" and node.name[1:].isdigit() and int(node.name[1]) <= level:
            break
        text = _text(node)
        if node.name in ("li", "dt", "strong") or (node.name[0] == "h" and int(node.name[1]) > level):
            short = text.split(" — ")[0].split(":")[0].strip()
            if 2 <= len(short) <= 80 and short not in items:
                items.append(short)
        if len(items) >= limit:
            break
    return items


def extract_company_page(html: str, url: str) -> dict[str, Any]:
    """Deterministic extracts from one official page: stated values, mission sentences, benefits, a short excerpt."""
    raw = BeautifulSoup(html or "", "html.parser")
    meta = raw.find("meta", attrs={"name": "description"}) or raw.find("meta", attrs={"property": "og:description"})
    title = _text(raw.title) if raw.title else ""
    soup = _clean_soup(html)
    values: list[dict[str, str]] = []
    benefits: list[str] = []
    for heading in soup.find_all(["h1", "h2", "h3", "h4"]):
        text = _text(heading)
        if len(text) <= 80 and VALUE_HEADINGS.search(text):
            values.extend({"value": v, "heading": text} for v in _after_heading(heading, 12)
                          if v.lower() != text.lower())
        elif len(text) <= 80 and BENEFIT_HEADINGS.search(text):
            benefits.extend(_after_heading(heading, 12))
    body = _text(soup.body or soup)
    missions = []
    for sentence in re.split(r"(?<=[.!?])\s+", body):
        if MISSION_WORDS.search(sentence) and 20 <= len(sentence) <= 400 and sentence not in missions:
            missions.append(sentence)
        if len(missions) >= 2:
            break
    return {"url": url, "title": title[:200], "description": (meta.get("content") or "")[:400] if meta else "",
            "values": values[:15], "missions": missions, "benefits": benefits[:12], "excerpt": body[:EXTRACT_CHARS],
            "links": [urljoin(url, a.get("href")) for a in raw.find_all("a", href=True)][:400]}


def candidate_pages(home: str, links: list[str]) -> list[str]:
    """Pages of the same site worth reading, the site's own links first, then the usual paths."""
    domain = matching.domain_of(home)
    found: list[str] = []
    for link in links:
        parsed = urlparse(link)
        if parsed.scheme not in ("http", "https") or not matching.same_domain(matching.domain_of(link), domain):
            continue
        path = parsed.path.lower()
        clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")
        if any(hint in path for hint in PAGE_HINTS) and clean not in found and path.count("/") <= 3:
            found.append(clean)
    base = f"{urlparse(home).scheme}://{urlparse(home).netloc}"
    for path in FALLBACK_PATHS:
        if not any(urlparse(f).path.rstrip("/").lower() == path for f in found):
            found.append(base + path)
    return found


class OfficialCompanySource:
    """C. The company's own website: what it says its mission and values are. Official values are kept apart from
    anything employees report (culture themes come from review sources only)."""

    name, label, source_type, company_level = "official_company", "Official company site", "OFFICIAL_COMPANY", True
    cacheable, ttl = True, "company"

    def configured(self) -> str | None:
        return None

    def fetch(self, snapshot: Snapshot) -> SourceResult:
        if not snapshot.website:
            return SourceResult(self.name, NOT_CONFIGURED,
                                detail="No company website is known: add it in Job Insights (it is never guessed)")
        from app.services.external_api import external_client
        from app.services.url_safety import UnsafeURL

        home = snapshot.website if "://" in snapshot.website else f"https://{snapshot.website}"
        allow_private = get_settings().jd_fetch_allow_private_hosts
        pages: list[dict[str, Any]] = []
        blocked = failed = 0
        headers = {"Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.9"}
        with external_client("WEB", service="job_insights", operation="official_company_page", timeout=20,
                             headers=headers, follow_redirects=False, user_id=snapshot.user_id,
                             job_id=snapshot.job_id, workflow_type="job",
                             metadata={"worker": "JOB_INSIGHTS", "source": self.name}) as client:
            queue, seen = [home], set()
            while queue and len(seen) < MAX_PAGES:
                url = queue.pop(0)
                if url in seen:
                    continue
                seen.add(url)
                try:
                    status, final, html = _get(client, url, allow_private=allow_private)
                except (UnsafeURL, httpx.HTTPError):
                    failed += 1
                    continue
                if status == 404 or not html:
                    continue
                verdict = jd_quality.classify_page(status=status, text=_text(_clean_soup(html))[:20000])
                if verdict in (jd_quality.LOGIN_REQUIRED, jd_quality.CAPTCHA_OR_ANTI_BOT, jd_quality.ACCESS_DENIED) \
                        or status == 429:
                    blocked += 1
                    continue
                if status >= 400:
                    failed += 1
                    continue
                page = extract_company_page(html, final)
                pages.append(page)
                if url == home:
                    queue.extend(candidate_pages(final, page["links"]))
        observations = self._observations(pages, snapshot)
        if observations:
            return SourceResult(self.name, OK, observations, detail=f"{len(pages)} page(s) read")
        if not pages and blocked:
            return SourceResult(self.name, BLOCKED, detail="The site asked for a login or a human check; not bypassed")
        if not pages and failed:
            return SourceResult(self.name, FAILED, detail="The company website could not be reached")
        return SourceResult(self.name, NO_DATA, detail="No mission, values or company description found on the site")

    def _observations(self, pages: list[dict[str, Any]], snapshot: Snapshot) -> list[ObservationIn]:
        out: list[ObservationIn] = []
        seen_values: set[str] = set()
        common = {"provider": "WEB", "recency": "UNKNOWN", "match_scope": "COMPANY",
                  "match_reason": "The company's own website"}
        for page in pages:
            url = page["url"][:1200]
            for item in page["values"]:
                key = item["value"].lower()
                if key in seen_values:
                    continue
                seen_values.add(key)
                out.append(ObservationIn("VALUES", "OFFICIAL_COMPANY", item, 0.9, title=page["title"] or "Values",
                                         source_url=url, **common))
            for sentence in page["missions"]:
                out.append(ObservationIn("COMPANY", "OFFICIAL_COMPANY", {"field": "mission", "text": sentence}, 0.85,
                                         title=page["title"] or "Mission", source_url=url, **common))
            if page["description"]:
                out.append(ObservationIn("COMPANY", "OFFICIAL_COMPANY",
                                         {"field": "description", "text": page["description"]}, 0.8,
                                         title=page["title"] or "Company description", source_url=url, **common))
            for benefit in page["benefits"]:
                out.append(ObservationIn("COMPANY", "OFFICIAL_COMPANY", {"field": "benefit", "text": benefit}, 0.8,
                                         title=page["title"] or "Benefits", source_url=url, **common))
            if page["excerpt"]:
                out.append(ObservationIn("COMPANY", "OFFICIAL_COMPANY", {"field": "page_extract",
                                                                           "text": page["excerpt"]}, 0.7,
                                         title=page["title"] or url, source_url=url, **common))
        return out


class PublicWebSource:
    """F. Public web search. No search provider is integrated, so this is always NOT_CONFIGURED: it is listed so the
    screen can say why nothing came from the wider web."""

    name, label, source_type, company_level = "public_web", "Public web", "PUBLIC_WEB", True

    def configured(self) -> str | None:
        return "No public web search provider is configured"

    def fetch(self, snapshot: Snapshot) -> SourceResult:
        return SourceResult(self.name, NOT_CONFIGURED, detail=self.configured())
