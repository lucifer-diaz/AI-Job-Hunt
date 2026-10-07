"""Job Insights sources that run an Apify actor: Glassdoor salaries, interviews and reviews, and PayScale salaries.

No actor ID is built in. Store actors and their input and output schemas change, so an admin configures each one
(APIFY_GLASSDOOR_COMPENSATION_ACTOR, APIFY_GLASSDOOR_INTERVIEW_ACTOR, APIFY_GLASSDOOR_REVIEWS_ACTOR,
APIFY_PAYSCALE_ACTOR) after checking it, and optionally its input (APIFY_*_INPUT: a JSON template whose strings may use
{company}, {role}, {location} and {limit}). A source without an actor, or an account without an Apify key, is
NOT_CONFIGURED and research carries on without it.

Runs use Apify's documented run API with the account's own token, through the shared instrumented client
(app.services.sources.apify_client → app.services.external_api): start the run, wait for it, read its dataset. The run
and dataset ids are kept as the observation's reference. Output is read defensively through field aliases; a dataset
whose items match none of them is FAILED ("schema not recognized"), never guessed at. Every item is matched to this
job's company, role and location (app.services.job_insights.matching) before it is used, and only short extracts are
kept: an interview question, a salary range, aggregate ratings, a review's headline — never a review copied whole.
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

import httpx

from app.services.job_insights import matching
from app.services.job_insights.base import (
    FAILED,
    NEEDS_REVIEW,
    NO_DATA,
    NOT_CONFIGURED,
    OK,
    ObservationIn,
    Snapshot,
    SourceResult,
)
from app.settings import get_settings

DEFAULT_LIMIT = 50
TERMINAL = {"SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"}
SHORT = 160  # the longest review or process text kept from one item

COMPANY_KEYS = ("employerName", "companyName", "company", "employer.name", "employer", "company.name")
DOMAIN_KEYS = ("website", "companyWebsite", "employerWebsite", "employer.website", "company.website", "domain")
TITLE_KEYS = ("jobTitle", "title", "job_title", "role", "position", "occupation")
LOCATION_KEYS = ("location", "city", "locationName", "metro", "country", "jobLocation")
DATE_KEYS = ("date", "reviewDate", "reportedAt", "updatedAt", "lastUpdated", "datePosted", "interviewDate",
             "submittedAt")
URL_KEYS = ("url", "link", "sourceUrl", "pageUrl")
CURRENCY_KEYS = ("currency", "currencyCode", "salaryCurrency", "payCurrency", "payCurrency.code", "currency.code")
PERIOD_KEYS = ("payPeriod", "period", "salaryPeriod", "interval", "unit", "payFrequency")
SAMPLE_KEYS = ("sampleSize", "salaryCount", "count", "numberOfSalaries", "dataPoints", "salariesCount")
BASE_KEYS = (("basePayLow", "basePayHigh", "basePayMedian"), ("minBasePay", "maxBasePay", "medianBasePay"))
TOTAL_KEYS = (("totalPayLow", "totalPayHigh", "totalPayMedian"), ("minTotalPay", "maxTotalPay", "medianTotalPay"),
              ("totalCompensationMin", "totalCompensationMax", "totalCompensationMedian"))
LOW_KEYS = ("min", "minSalary", "salaryMin", "lowSalary", "low", "payPercentile10", "percentile10", "p10", "salaryLow")
HIGH_KEYS = ("max", "maxSalary", "salaryMax", "highSalary", "high", "payPercentile90", "percentile90", "p90",
             "salaryHigh")
MEDIAN_KEYS = ("median", "medianSalary", "payPercentile50", "percentile50", "p50", "averageSalary", "avgSalary",
               "average", "mean")
QUESTION_KEYS = ("question", "interviewQuestion", "questionText")
QUESTIONS_KEYS = ("questions", "interviewQuestions")
PROCESS_KEYS = ("process", "interviewProcess", "processDescription", "interviewDescription")
RATING_KEYS = {"overall_rating": ("overallRating", "rating", "ratingOverall"),
               "career_opportunities": ("careerOpportunitiesRating", "ratingCareerOpportunities"),
               "compensation_rating": ("compensationAndBenefitsRating", "ratingCompensationAndBenefits"),
               "work_life_balance": ("workLifeBalanceRating", "ratingWorkLifeBalance"),
               "management": ("seniorManagementRating", "ratingSeniorLeadership", "seniorLeadershipRating"),
               "culture": ("cultureAndValuesRating", "ratingCultureAndValues")}


def _get(item: dict[str, Any], key: str) -> Any:
    node: Any = item
    for part in key.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def first(item: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = _get(item, key)
        if value not in (None, "", [], {}):
            return value
    return None


def _text(value: Any, limit: int = SHORT) -> str | None:
    if isinstance(value, dict):
        value = value.get("name") or value.get("text")
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text[:limit] or None


def _num(value: Any) -> float | None:
    from app.services.compensation import _number

    return _number(value)


def render_input(template: str, values: dict[str, Any]) -> dict[str, Any]:
    """The admin's JSON input template with {company}, {role}, {location} and {limit} filled in (strings only)."""
    def fill(node: Any) -> Any:
        if isinstance(node, str):
            if node in ("{limit}",):
                return values["limit"]
            out = node
            for key, value in values.items():
                out = out.replace("{" + key + "}", str(value))
            return out
        if isinstance(node, list):
            return [fill(v) for v in node]
        if isinstance(node, dict):
            return {k: fill(v) for k, v in node.items()}
        return node
    parsed = json.loads(template)
    if not isinstance(parsed, dict):
        raise TypeError("The actor input template must be a JSON object")
    return fill(parsed)


def default_input(values: dict[str, Any]) -> dict[str, Any]:
    """A generic input for an actor without a configured template. Field names differ between actors, so the README
    says to set APIFY_*_INPUT; most actors ignore fields they do not define."""
    return {"companyName": values["company"], "company": values["company"], "jobTitle": values["role"],
            "query": f"{values['company']} {values['role']}".strip(), "location": values["location"],
            "maxItems": values["limit"]}


class ActorRunFailed(Exception):
    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def run_actor(token: str, actor_id: str, actor_input: dict[str, Any], *, operation: str, user_id: int, job_id: int,
              timeout_seconds: int, limit: int) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Start the actor, wait for it (bounded) and read its dataset. Returns (items, {run_id, dataset_id})."""
    from app.services.sources import APIFY_API, _apify_actor_api_id, apify_client

    deadline = time.monotonic() + max(10, timeout_seconds)
    meta = {"actor_id": actor_id, "workflow": "job_insights", "job_id": job_id}
    with apify_client(token, operation=operation, user_id=user_id, metadata=meta, json_body=True,
                      timeout=min(70, max(10, timeout_seconds)), service="job_insights", job_id=job_id,
                      workflow_type="job") as client:
        wait = max(1, min(60, int(deadline - time.monotonic())))
        response = client.post(f"{APIFY_API}/acts/{_apify_actor_api_id(actor_id)}/runs",
                               params={"waitForFinish": wait}, json=actor_input,
                               extensions={"operation": f"{operation}_run", "metadata": meta})
        if response.status_code == 404:
            raise ActorRunFailed("Actor not found (check the configured actor ID)")
        if response.status_code == 429:
            raise ActorRunFailed("Apify rate limited the request")
        response.raise_for_status()
        run = (response.json() or {}).get("data") or {}
        run_id, dataset_id = run.get("id"), run.get("defaultDatasetId")
        status = run.get("status")
        while status not in TERMINAL and run_id and time.monotonic() < deadline:
            wait = max(1, min(60, int(deadline - time.monotonic())))
            poll = client.get(f"{APIFY_API}/actor-runs/{run_id}", params={"waitForFinish": wait},
                              extensions={"operation": f"{operation}_status", "metadata": {**meta, "run_id": run_id}})
            poll.raise_for_status()
            run = (poll.json() or {}).get("data") or {}
            status, dataset_id = run.get("status"), run.get("defaultDatasetId") or dataset_id
        reference = {"run_id": str(run_id or ""), "dataset_id": str(dataset_id or "")}
        if status != "SUCCEEDED":
            raise ActorRunFailed(f"Actor run {str(status or 'did not finish').lower()} "
                                 f"(run {run_id or 'unknown'})" if status in TERMINAL else
                                 "Actor run did not finish in time")
        if not dataset_id:
            return [], reference
        items = client.get(f"{APIFY_API}/datasets/{dataset_id}/items", params={"clean": "true", "limit": limit},
                           extensions={"operation": f"{operation}_dataset",
                                       "metadata": {**meta, "run_id": run_id, "dataset_id": dataset_id}})
        items.raise_for_status()
        data = items.json()
    return [i for i in data if isinstance(i, dict)] if isinstance(data, list) else [], reference


# ───────────── item normalizers: return [(category, data, title, location, company, domain, date, url)] ─────────────

Parsed = dict[str, Any]


def _context(item: dict[str, Any]) -> Parsed:
    return {"company": _text(first(item, COMPANY_KEYS), 300), "domain": matching.domain_of(_text(first(
        item, DOMAIN_KEYS), 300)), "title": _text(first(item, TITLE_KEYS), 300),
        "location": _text(first(item, LOCATION_KEYS), 200), "observed_at": matching.parse_date(first(item, DATE_KEYS)),
        "url": _text(first(item, URL_KEYS), 1200)}


def salary_items(item: dict[str, Any]) -> list[Parsed]:
    """Salary observations in one item; base and total pay become separate observations, never merged."""
    out = []
    shared = {"currency": _text(first(item, CURRENCY_KEYS), 10), "period": _text(first(item, PERIOD_KEYS), 30),
              "sample_size": int(n) if (n := _num(first(item, SAMPLE_KEYS))) else None}
    for basis, groups in (("BASE", BASE_KEYS), ("TOTAL_COMPENSATION", TOTAL_KEYS)):
        for low_key, high_key, median_key in groups:
            low, high, median = _num(item.get(low_key)), _num(item.get(high_key)), _num(item.get(median_key))
            if low or high or median:
                out.append({**shared, "low": low, "high": high, "median": median, "basis": basis})
                break
    if not out:
        low, high, median = (_num(first(item, LOW_KEYS)), _num(first(item, HIGH_KEYS)),
                             _num(first(item, MEDIAN_KEYS)))
        if low or high or median:
            basis_text = _text(first(item, ("payType", "basis", "compType", "salaryType")), 40)
            out.append({**shared, "low": low, "high": high, "median": median, "basis": basis_text or "BASE"})
    return [{"category": "COMPENSATION", "data": d, **_context(item)} for d in out]


def interview_items(item: dict[str, Any]) -> list[Parsed]:
    ctx = _context(item)
    out: list[Parsed] = []
    questions: list[str] = []
    single = first(item, QUESTION_KEYS)
    if isinstance(single, str):
        questions.append(single)
    many = first(item, QUESTIONS_KEYS)
    if isinstance(many, list):
        questions.extend(q if isinstance(q, str) else (q.get("question") or q.get("text") or "")
                         for q in many if isinstance(q, (str, dict)))
    stage = _text(first(item, ("stage", "interviewStage", "round")), 80)
    for question in questions:
        text = _text(question, 300)
        if text and len(text) >= 8:
            out.append({"category": "INTERVIEW_QUESTION", "data": {"question": text, "stage": stage}, **ctx})
    process = _text(first(item, PROCESS_KEYS), SHORT)
    difficulty = _text(first(item, ("difficulty", "interviewDifficulty")), 40)
    duration = _text(first(item, ("duration", "processDuration", "interviewDuration")), 60)
    if process or difficulty or duration:
        out.append({"category": "INTERVIEW_PROCESS", "data": {"text": process, "difficulty": difficulty,
                                                              "duration": duration,
                                                              "outcome": _text(first(item, ("outcome", "offer")), 40)},
                    **ctx})
    return out


def review_items(item: dict[str, Any]) -> list[Parsed]:
    ctx = _context(item)
    out: list[Parsed] = []
    ratings = {name: _num(first(item, keys)) for name, keys in RATING_KEYS.items()}
    ratings = {k: v for k, v in ratings.items() if v is not None and v <= 5}
    count = _num(first(item, ("numberOfRatings", "reviewCount", "reviewsCount", "ratingCount")))
    if ratings:
        out.append({"category": "SENTIMENT", "data": {**ratings, "sample_size": int(count) if count else None,
                                                      "aggregate": bool(count)}, **ctx})
    headline = _text(first(item, ("headline", "summary", "reviewTitle")))
    pros, cons = _text(first(item, ("pros", "advantages"))), _text(first(item, ("cons", "disadvantages")))
    if headline or pros or cons:
        out.append({"category": "CULTURE", "data": {"headline": headline, "pros": pros, "cons": cons,
                                                    "rating": ratings.get("overall_rating")}, **ctx})
    return out


class ApifyInsightSource:
    """One configured actor answering one kind of question. Subclasses name the actor and the item normalizer."""

    name = label = source_type = ""
    actor_setting = input_setting = operation = ""
    company_level = False
    compensation = False
    cacheable, ttl = True, "interview"  # which *_INSIGHT_TTL_DAYS reuse follows
    normalize: Callable[[dict[str, Any]], list[Parsed]] = staticmethod(lambda item: [])
    base_confidence = 0.75

    def actor_id(self) -> str:
        return (getattr(get_settings(), self.actor_setting, "") or "").strip()

    def configured(self) -> str | None:
        if not self.actor_id():
            return f"No actor configured ({self.actor_setting.upper()})"
        return None

    def fetch(self, snapshot: Snapshot) -> SourceResult:
        reason = self.configured()
        if reason:
            return SourceResult(self.name, NOT_CONFIGURED, detail=reason)
        if not snapshot.apify_token:
            return SourceResult(self.name, NOT_CONFIGURED, detail="Add your Apify key in Settings")
        settings = get_settings()
        values = {"company": snapshot.company, "role": snapshot.title, "location": snapshot.location,
                  "limit": DEFAULT_LIMIT}
        template = (getattr(settings, self.input_setting, "") or "").strip()
        try:
            actor_input = render_input(template, values) if template else default_input(values)
        except (ValueError, TypeError) as exc:
            return SourceResult(self.name, FAILED, detail=f"The configured actor input is not valid JSON: {exc}"[:200])
        actor_id = self.actor_id()
        try:
            items, reference = run_actor(snapshot.apify_token, actor_id, actor_input, operation=self.operation,
                                         user_id=snapshot.user_id, job_id=snapshot.job_id,
                                         timeout_seconds=settings.job_insights_source_timeout_seconds,
                                         limit=DEFAULT_LIMIT)
        except ActorRunFailed as exc:
            return SourceResult(self.name, FAILED, detail=exc.detail)
        except httpx.TimeoutException:
            return SourceResult(self.name, FAILED, detail="Apify did not answer in time")
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            return SourceResult(self.name, FAILED, detail="Apify rejected the Apify key" if code in (401, 403)
                                else "Apify rate limited the request" if code == 429 else f"Apify answered {code}")
        except (httpx.HTTPError, ValueError) as exc:
            return SourceResult(self.name, FAILED, detail=f"Apify request failed ({type(exc).__name__})")
        if not items:
            return SourceResult(self.name, NO_DATA, detail="The actor returned no results")
        return self._match(snapshot, items, actor_id, reference)

    def _match(self, snapshot: Snapshot, items: list[dict[str, Any]], actor_id: str,
               reference: dict[str, str]) -> SourceResult:
        parsed = [p for item in items for p in type(self).normalize(item)]
        if not parsed:
            return SourceResult(self.name, FAILED, detail="Schema not recognized: the actor's items have none of the "
                                                          "fields this source reads (check the actor or its input)")
        out: list[ObservationIn] = []
        rejected = review = 0
        max_age = get_settings().insight_max_observation_age_months
        ref = f"run:{reference.get('run_id', '')} dataset:{reference.get('dataset_id', '')}"[:300]
        key = matching.cache_key(self.name, snapshot.target, company_level=self.company_level)
        for p in parsed:
            match = matching.assess(snapshot.target, base_confidence=self.base_confidence, company=p["company"],
                                    domain=p["domain"], title=p["title"], location=p["location"],
                                    observed_at=p["observed_at"], now=snapshot.now, compensation=self.compensation,
                                    company_level=self.company_level or p["category"] in ("SENTIMENT", "CULTURE"),
                                    max_age_months=max_age)
            if match.status == matching.REJECTED:
                rejected += 1
                continue
            if match.status == matching.NEEDS_REVIEW:
                review += 1
            out.append(ObservationIn(
                p["category"], self.source_type, p["data"], match.confidence if match.status == matching.ACCEPTED
                else 0.0, title=" · ".join(x for x in (p["company"], p["title"], p["location"]) if x)[:300] or None,
                source_url=p["url"], source_reference=ref, provider="APIFY", actor_id=actor_id[:300],
                observed_at=p["observed_at"], match_scope=match.scope, match_status=match.status,
                match_reason=match.reason[:300], recency=match.recency, cache_key=key))
        accepted = [o for o in out if o.match_status == matching.ACCEPTED]
        if accepted:
            return SourceResult(self.name, OK, out, rejected=rejected, review=review,
                                detail=f"{len(accepted)} matching result(s)")
        if review:
            return SourceResult(self.name, NEEDS_REVIEW, out, rejected=rejected, review=review,
                                detail="Results found, but nothing confirmed they are about this company")
        return SourceResult(self.name, NO_DATA, rejected=rejected,
                            detail=f"No result matched this company, role and location ({rejected} set aside)")


class GlassdoorCompensationSource(ApifyInsightSource):
    name, label, source_type = "glassdoor_compensation", "Glassdoor salaries", "GLASSDOOR"
    actor_setting, input_setting = "apify_glassdoor_compensation_actor", "apify_glassdoor_compensation_input"
    operation, compensation, base_confidence, ttl = "glassdoor_compensation", True, 0.8, "compensation"
    normalize = staticmethod(salary_items)


class GlassdoorInterviewSource(ApifyInsightSource):
    name, label, source_type = "glassdoor_interview", "Glassdoor interviews", "GLASSDOOR"
    actor_setting, input_setting = "apify_glassdoor_interview_actor", "apify_glassdoor_interview_input"
    operation, base_confidence = "glassdoor_interview", 0.75
    normalize = staticmethod(interview_items)


class GlassdoorReviewsSource(ApifyInsightSource):
    name, label, source_type = "glassdoor_reviews", "Glassdoor reviews", "GLASSDOOR"
    actor_setting, input_setting = "apify_glassdoor_reviews_actor", "apify_glassdoor_reviews_input"
    operation, company_level, base_confidence, ttl = "glassdoor_reviews", True, 0.7, "company"
    normalize = staticmethod(review_items)


class PayScaleSource(ApifyInsightSource):
    name, label, source_type = "payscale", "PayScale", "PAYSCALE"
    actor_setting, input_setting = "apify_payscale_actor", "apify_payscale_input"
    operation, compensation, base_confidence, ttl = "payscale_compensation", True, 0.7, "compensation"
    normalize = staticmethod(salary_items)
