"""Finding the job description in a posting page, without a model.

    page HTML (from plain HTTP, or rendered by the browser)
      → site extractor for the URL (Greenhouse, Lever, Ashby, Workable, LinkedIn, Indeed), else Generic
      → JSON-LD JobPosting, then known selectors, then scored DOM blocks
      → Extraction: the text, how it was found, a confidence, and the runner-up blocks

Every extractor works on HTML, so the text a browser renders goes through exactly the same code as the text HTTP
fetched. Nothing here fetches anything. `candidate_blocks` is what an ambiguous page hands to the model fallback:
the model chooses among these blocks and never writes text of its own.

Selectors are deliberately few. A site adapter only adds what the generic extractor would miss (a public JSON
endpoint the board documents, or the one container its postings use); everything else falls through to Generic.
"""
from __future__ import annotations

import html as html_lib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag

from app.services import jd_quality
from app.services.job_description import normalize_description

MAX_TEXT = 30000
MIN_BLOCK_CHARS = 200
MAX_CANDIDATES = 8
# Attribute fragments that name a description container on most sites.
DESCRIPTION_HINTS = ("job-description", "jobdescription", "job_description", "description", "job-details",
                     "jobdetails", "job-content", "posting-description", "role-description", "job-body",
                     "posting-body", "job__description", "jobsearch-jobdescriptiontext", "show-more-less-html",
                     "details-pane", "job-info", "vacancy-description", "position-description")
NOISE_HINTS = ("cookie", "consent", "banner", "footer", "header", "navbar", "nav-", "menu", "breadcrumb", "sidebar",
               "related", "similar", "recommend", "share", "social", "newsletter", "modal", "popup", "signin",
               "login", "apply-form", "application-form")
REMOVED_TAGS = ("script", "style", "noscript", "svg", "nav", "footer", "header", "aside", "form", "iframe", "button",
                "select", "template", "input", "textarea", "img", "picture", "video", "canvas")
BLOCK_TAGS = ("main", "article", "section", "div", "td", "ul", "ol")
HEADING_WORDS = ("about the role", "about the job", "responsibilities", "what you'll do", "what you will do",
                 "requirements", "qualifications", "preferred qualifications", "skills", "experience", "about you",
                 "who you are", "what we offer", "benefits", "nice to have", "your role", "the role", "job summary",
                 "minimum qualifications", "basic qualifications", "key responsibilities")


@dataclass
class Block:
    """One candidate container of posting text."""

    id: str
    text: str
    score: float
    selector: str = ""
    order: int = 0

    def public(self, limit: int = 4000) -> dict[str, Any]:
        return {"id": self.id, "text": self.text[:limit]}


@dataclass
class Extraction:
    text: str = ""
    method: str = ""  # json_ld | api | selector | dom_score | none
    extractor: str = "generic"
    confidence: float = 0.0
    title: str = ""
    candidates: list[Block] = field(default_factory=list)
    has_password_field: bool = False
    page_text: str = ""  # visible text of the whole page, for classifying pages that are not postings

    @property
    def found(self) -> bool:
        return bool(self.text.strip())


def html_to_text(fragment: str) -> str:
    """Posting HTML as clean text, keeping headings, paragraphs and bullets (app.services.job_description)."""
    return normalize_description(fragment or "")[:MAX_TEXT]


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html or "", "html.parser")


def _title(soup: BeautifulSoup) -> str:
    return soup.title.get_text(" ", strip=True) if soup.title else ""


def _attrs(tag: Tag) -> str:
    classes = tag.get("class") or []
    parts = [tag.get("id") or "", " ".join(classes) if isinstance(classes, list) else str(classes),
             tag.get("data-testid") or "", tag.get("data-automation-id") or "", tag.get("data-qa") or "",
             tag.get("data-ui") or "", tag.get("itemprop") or "", tag.get("role") or ""]
    return " ".join(str(p) for p in parts).lower()


# ───────────── JSON-LD JobPosting ─────────────

def _json_ld_items(soup: BeautifulSoup) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                if "@graph" in node:
                    stack.extend(node["@graph"] if isinstance(node["@graph"], list) else [node["@graph"]])
                kind = node.get("@type")
                if kind == "JobPosting" or (isinstance(kind, list) and "JobPosting" in kind):
                    items.append(node)
    return items


def json_ld_description(soup: BeautifulSoup) -> str:
    for item in _json_ld_items(soup):
        description = item.get("description")
        if isinstance(description, str) and description.strip():
            # Often HTML, sometimes entity-escaped HTML.
            raw = html_lib.unescape(description) if "&lt;" in description else description
            return html_to_text(raw)
    return ""


# ───────────── scoring DOM blocks ─────────────

def _clean(soup: BeautifulSoup) -> BeautifulSoup:
    for tag in soup(list(REMOVED_TAGS)):
        tag.decompose()
    for tag in soup.find_all(attrs={"role": ["navigation", "banner", "contentinfo", "dialog", "search"]}):
        tag.decompose()
    for tag in soup.find_all(attrs={"aria-hidden": "true"}):
        tag.decompose()
    for tag in list(soup.find_all(True)):
        if getattr(tag, "decomposed", False):
            continue  # inside a container removed a moment ago
        attrs = _attrs(tag)
        if attrs and any(hint in attrs for hint in NOISE_HINTS) and not any(h in attrs for h in DESCRIPTION_HINTS):
            tag.decompose()
    return soup


def _link_density(tag: Tag, text_length: int) -> float:
    if not text_length:
        return 1.0
    linked = sum(len(a.get_text(" ", strip=True)) for a in tag.find_all("a"))
    return min(1.0, linked / text_length)


def score_block(tag: Tag, text: str) -> float:
    """Deterministic score for one container: useful length, posting headings and keywords, list structure; minus
    link density and site boilerplate. Higher is more likely the job description."""
    length = len(text)
    if length < MIN_BLOCK_CHARS:
        return 0.0
    lowered = text.lower()
    found, strong = jd_quality.signals(text)
    headings = sum(1 for h in tag.find_all(["h1", "h2", "h3", "h4", "strong", "b"])
                   if any(word in h.get_text(" ", strip=True).lower() for word in HEADING_WORDS))
    items = len(tag.find_all("li"))
    attrs = _attrs(tag)
    score = math.log2(min(length, 12000)) * 2  # grows slowly: a longer block is not much better past a page
    score += min(found, 15) * 0.8 + min(strong, 8) * 2.5 + min(headings, 8) * 2 + min(items, 25) * 0.25
    if any(hint in attrs for hint in DESCRIPTION_HINTS):
        score += 8
    if tag.name in ("main", "article"):
        score += 2
    score -= _link_density(tag, length) * 25
    score -= jd_quality.boilerplate_ratio(text) * 20
    score -= sum(1 for word in jd_quality.BOILERPLATE_WORDS if word in lowered) * 1.5
    return round(score, 3)


def scored_blocks(soup: BeautifulSoup) -> list[Block]:
    """Candidate blocks, best first, with nested duplicates collapsed to the most specific useful container."""
    found: list[tuple[Tag, Block]] = []
    for order, tag in enumerate(soup.find_all(list(BLOCK_TAGS))):
        text = html_to_text(str(tag))
        score = score_block(tag, text)
        if score > 0:
            found.append((tag, Block(id="", text=text, score=score, selector=_attrs(tag)[:80], order=order)))
    found.sort(key=lambda pair: pair[1].score, reverse=True)
    chosen: list[tuple[Tag, Block]] = []
    for tag, block in found:
        # A block that sits inside (or around) a better one adds nothing new unless it covers different text.
        overlaps = [c for c, _ in chosen if tag in c.descendants or c in tag.descendants]
        if overlaps:
            continue
        chosen.append((tag, block))
        if len(chosen) >= MAX_CANDIDATES:
            break
    blocks = [block for _, block in chosen]
    for index, block in enumerate(blocks, start=1):
        block.id = f"b{index}"
    return blocks


def confidence_of(best: Block | None, runner_up: Block | None, text: str) -> float:
    """How sure the DOM pick is: it validates, and it clearly beats the next block."""
    if best is None:
        return 0.0
    base = 0.55 if jd_quality.validate_recovered(text).ok else 0.2
    margin = (best.score - runner_up.score) / max(best.score, 1.0) if runner_up else 1.0
    return round(min(0.95, base + max(0.0, margin) * 0.4), 3)


# ───────────── extractors ─────────────

class JobDescriptionExtractor:
    """One way to find a posting's description. Subclasses add a site's structured endpoint or containers."""

    name = "generic"
    hosts: tuple[str, ...] = ()
    selectors: tuple[str, ...] = ()

    def supports(self, url: str) -> bool:
        host = (urlparse(url or "").hostname or "").lower()
        return any(host == h or host.endswith("." + h) for h in self.hosts)

    def structured_url(self, url: str) -> str | None:
        """A documented public JSON endpoint for this posting, tried before the page itself (None when none)."""
        return None

    def from_structured(self, payload: Any) -> str:
        """The description from the structured endpoint's JSON."""
        return ""

    def extract(self, html: str, url: str = "") -> Extraction:
        soup = _soup(html)
        title = _title(soup)
        has_password = soup.find("input", attrs={"type": "password"}) is not None
        described = json_ld_description(soup)
        cleaned = _clean(soup)
        page_text = html_to_text(str(cleaned.body or cleaned))
        blocks = scored_blocks(cleaned)
        result = Extraction(extractor=self.name, title=title, candidates=blocks, has_password_field=has_password,
                            page_text=page_text)
        if described and jd_quality.validate_recovered(described).ok:
            result.text, result.method, result.confidence = described, "json_ld", 0.95
            return result
        for selector in self.selectors:
            node = cleaned.select_one(selector)
            text = html_to_text(str(node)) if node is not None else ""
            if text and jd_quality.validate_recovered(text).ok:
                result.text, result.method, result.confidence = text, "selector", 0.9
                return result
        if blocks:
            best, runner_up = blocks[0], blocks[1] if len(blocks) > 1 else None
            result.text, result.method = best.text, "dom_score"
            result.confidence = self.confidence(best, runner_up)
            return result
        if described:  # present but too thin to validate; better than nothing for the caller's checks
            result.text, result.method, result.confidence = described, "json_ld", 0.3
        return result

    def confidence(self, best: Block, runner_up: Block | None) -> float:
        return confidence_of(best, runner_up, best.text)


class GenericExtractor(JobDescriptionExtractor):
    name = "generic"

    def supports(self, url: str) -> bool:
        return True


class GreenhouseExtractor(JobDescriptionExtractor):
    name = "greenhouse"
    hosts = ("greenhouse.io",)
    selectors = ("#content", ".job__description", ".job-post-content", "#app_body .content")
    _path = re.compile(r"^/(?:embed/job_app\?for=)?([\w-]+)/jobs/(\d+)")

    def structured_url(self, url: str) -> str | None:
        parsed = urlparse(url)
        match = self._path.match(parsed.path)
        if match and (parsed.hostname or "").startswith(("boards.", "job-boards.")):
            return f"https://boards-api.greenhouse.io/v1/boards/{match.group(1)}/jobs/{match.group(2)}"
        return None

    def from_structured(self, payload: Any) -> str:
        content = (payload or {}).get("content") if isinstance(payload, dict) else None
        return html_to_text(html_lib.unescape(content)) if isinstance(content, str) else ""


class LeverExtractor(JobDescriptionExtractor):
    name = "lever"
    hosts = ("lever.co",)
    selectors = ("[data-qa='job-description']", ".posting-page .content", ".section-wrapper.page-full-width")
    _path = re.compile(r"^/([\w.-]+)/([0-9a-f-]{36})")

    def structured_url(self, url: str) -> str | None:
        parsed = urlparse(url)
        match = self._path.match(parsed.path)
        if match and (parsed.hostname or "").startswith("jobs."):
            return f"https://api.lever.co/v0/postings/{match.group(1)}/{match.group(2)}"
        return None

    def from_structured(self, payload: Any) -> str:
        if not isinstance(payload, dict):
            return ""
        parts = [payload.get("descriptionPlain") or html_to_text(payload.get("description") or "")]
        for item in payload.get("lists") or []:
            if isinstance(item, dict):
                parts.append(f"{item.get('text', '')}:\n{html_to_text(item.get('content') or '')}")
        parts.append(payload.get("additionalPlain") or html_to_text(payload.get("additional") or ""))
        return "\n\n".join(p.strip() for p in parts if p and p.strip())[:MAX_TEXT]


class AshbyExtractor(JobDescriptionExtractor):
    name = "ashby"
    hosts = ("ashbyhq.com",)
    selectors = ("[class*='_descriptionText']", "[class*='description']", "#overview")


class WorkableExtractor(JobDescriptionExtractor):
    name = "workable"
    hosts = ("workable.com",)
    selectors = ("[data-ui='job-description']", "[data-ui='job-requirements']", "section[class*='description']")

    def extract(self, html: str, url: str = "") -> Extraction:
        result = super().extract(html, url)
        if result.method in ("json_ld", "selector"):
            return result
        # Workable splits a posting into description, requirements and benefits sections: keep all three.
        soup = _clean(_soup(html))
        parts = [html_to_text(str(node)) for key in ("job-description", "job-requirements", "job-benefits")
                 if (node := soup.select_one(f"[data-ui='{key}']")) is not None]
        text = "\n\n".join(p for p in parts if p)
        if text and jd_quality.validate_recovered(text).ok:
            result.text, result.method, result.confidence = text, "selector", 0.9
        return result


class LinkedInExtractor(JobDescriptionExtractor):
    name = "linkedin"
    hosts = ("linkedin.com",)
    selectors = (".show-more-less-html__markup", ".description__text", ".jobs-description__content",
                 ".jobs-box__html-content")


class IndeedExtractor(JobDescriptionExtractor):
    name = "indeed"
    hosts = ("indeed.com",)
    selectors = ("#jobDescriptionText", ".jobsearch-jobDescriptionText", "[data-testid='jobDescriptionText']")


EXTRACTORS: tuple[JobDescriptionExtractor, ...] = (
    GreenhouseExtractor(), LeverExtractor(), AshbyExtractor(), WorkableExtractor(), LinkedInExtractor(),
    IndeedExtractor())
GENERIC = GenericExtractor()


def extractor_for(url: str) -> JobDescriptionExtractor:
    """The site extractor for a URL; Generic for everything else."""
    return next((extractor for extractor in EXTRACTORS if extractor.supports(url)), GENERIC)


def extract(html: str, url: str = "") -> Extraction:
    return extractor_for(url).extract(html, url)


def assemble(blocks: list[Block], selected_ids: list[str]) -> str:
    """The text of the chosen blocks in page order. Only fetched text is ever returned: nothing is written."""
    chosen = {block_id for block_id in selected_ids}
    parts = [block for block in sorted(blocks, key=lambda b: b.order) if block.id in chosen]
    return "\n\n".join(block.text for block in parts)[:MAX_TEXT]


def likely_blocks(blocks: list[dict[str, Any]]) -> list[str]:
    """A deterministic pick for the no-cost mock model: the block with the strongest posting signals."""
    if not blocks:
        return []
    best = max(blocks, key=lambda b: (jd_quality.signals(b.get("text"))[1], jd_quality.signals(b.get("text"))[0],
                                      len(b.get("text") or "")))
    return [str(best.get("id"))]
