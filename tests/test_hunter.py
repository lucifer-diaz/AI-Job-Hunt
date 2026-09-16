"""Apify Hunter: search API, actor adapters, posting dates, deduplication, failures and scheduling.

Apify is never called: apify_post and apify_get are replaced by FakeApify, which records the actor
inputs and serves dataset items on demand.
"""
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.services.scheduled_jobs as sj
from app.db import SessionLocal
from app.main import app
from app.models import Job, SearchProfile, SearchRun, WorkflowTransition
from app.services.apify_adapters import (ADAPTERS, AdapterError, GlassdoorAdapter, HunterSearch, parse_posted_date,
                                         plan_runs)
from app.services.lifecycle import WorkflowState as S
from app.services.user_settings import set_user_keys
from app.settings import get_settings
from tests.conftest import ensure_test_user

client = TestClient(app)
TOKEN = "hunter-test-token"
REFERENCE = datetime(2026, 9, 16, 12, 0)

# Input properties each actor publishes in its input schema (read from the Apify API on 2026-09-16).
ACTOR_SCHEMAS = {
    "openclawai/job-board-scraper": {
        "searchTerm", "searchTerms", "location", "sites", "maxResults", "isRemote", "jobType", "hoursOld",
        "countryIndeed", "googleSearchTerm", "distance", "offset", "easyApply", "linkedinFetchDescription",
        "linkedinCompanyIds", "descriptionFormat", "enforceAnnualSalary", "userAgent", "caCert", "proxyConfiguration"},
    "thirdwatch/linkedin-jobs-scraper": {
        "queries", "location", "country", "companyName", "maxResultsPerQuery", "maxPages", "scrapeMode", "datePosted",
        "jobType", "experienceLevel", "proxyConfiguration"},
    "crawlerbros/indeed-jobs-scraper": {
        "position", "location", "country", "maxItemsPerSearch", "startUrls", "parseCompanyDetails",
        "saveOnlyUniqueItems", "followApplyRedirects"},
    "crawlerbros/glassdoor-jobs-scraper": {"keyword", "location", "companyUrl", "maxItems", "proxy"},
    "crawlerbros/naukri-scraper": {
        "searchUrl", "maxItems", "keyword", "location", "experienceYears", "salaryLakhs", "jobAge", "jobType",
        "workMode", "sortBy"},
}


class FakeApify:
    """Stands in for the Apify REST API: runs start immediately and finish when a test says so."""

    def __init__(self):
        self.started, self.runs, self.items, self.gets = [], {}, {}, []
        self.start_error = None  # callable(actor_input) -> Exception | None

    def post(self, path, payload, token=None):
        assert token == TOKEN, "searches must use the account's own Apify token"
        error = self.start_error(payload) if self.start_error else None
        if error:
            raise error
        run_id = uuid.uuid4().hex[:17]
        self.started.append({"path": path, "input": payload, "run_id": run_id})
        self.runs[run_id] = {"id": run_id, "status": "READY", "defaultDatasetId": f"ds{run_id}",
                             "startedAt": "2026-09-16T08:00:00.000Z"}
        self.items[f"ds{run_id}"] = []
        return {"data": dict(self.runs[run_id])}

    def get(self, path, params=None, token=None):
        assert token == TOKEN
        self.gets.append(path)
        if path.startswith("/actor-runs/"):
            run_id = path.rsplit("/", 1)[1]
            if run_id not in self.runs:  # a pending run left by another test
                raise httpx.ConnectError("unknown run")
            return {"data": dict(self.runs[run_id])}
        if path.startswith("/datasets/"):
            return self.items[path.split("/")[2]]
        raise AssertionError(f"unexpected Apify call {path}")

    def finish(self, run_id, items, status="SUCCEEDED", message=""):
        self.runs[run_id].update(status=status, statusMessage=message, finishedAt="2026-09-16T08:05:00.000Z")
        self.items[f"ds{run_id}"] = items
        return run_id

    def input_for(self, actor_fragment):
        return [run["input"] for run in self.started if actor_fragment in run["path"]]


@pytest.fixture
def apify(monkeypatch):
    fake = FakeApify()
    monkeypatch.setattr("app.services.sources.apify_post", fake.post)
    monkeypatch.setattr("app.services.sources.apify_get", fake.get)
    with SessionLocal() as db:
        set_user_keys(db, {"apify": TOKEN}, user_id=ensure_test_user().id)
    return fake


def tag():
    return uuid.uuid4().hex[:8]


def digits():
    return str(uuid.uuid4().int % 10**9 + 10**9)


def body(**overrides):
    payload = {"roles": ["Engineering Manager", "Head of Engineering"], "locations": ["Singapore"],
               "sites": ["linkedin", "indeed", "glassdoor"], "hours_old": 72, "max_results": 20}
    payload.update(overrides)
    return payload


def start(**overrides):
    response = client.post("/api/hunter/search", json=body(**overrides))
    assert response.status_code == 202, response.text
    return response.json()


def sync():
    response = client.post("/api/hunter/runs/sync")
    assert response.status_code == 200, response.text
    return response.json()


def run_detail(run_id):
    response = client.get(f"/api/hunter/runs/{run_id}")
    assert response.status_code == 200, response.text
    return response.json()


def inputs(search_payload):
    """The actor inputs a search plans, without touching the database."""
    runs, warnings = plan_runs(HunterSearch.from_dict(body(**search_payload)))
    return runs, warnings


def board_item(t, role="Engineering Manager", site="linkedin", **extra):
    number = digits()
    item = {"id": f"li-{number}" if site == "linkedin" else f"in-{number}", "site": site,
            "title": f"Engineering Manager {t}", "company": f"Acme {t}", "location": "Singapore, Singapore",
            "job_url": f"https://www.{site}.com/jobs/view/{number}", "date_posted": "2026-09-14",
            "matched_search_term": role, "scraped_at": "2026-09-16T08:03:00+00:00", "description": "Lead the team"}
    item.update(extra)
    return item


def jobs_for(company):
    with SessionLocal() as db:
        return db.scalars(select(Job).where(Job.company == company).order_by(Job.id)).all()


# ───────────── starting searches ─────────────

def test_search_starts_one_run_per_location_and_returns_immediately(apify):
    out = start(locations=["Singapore", "Dubai"], roles=["Engineering Manager"])

    assert out["started"] == 2 and out["failed"] == 0
    assert [run["location"] for run in out["runs"]] == ["Singapore", "Dubai"]
    assert all(run["status"] == "PENDING" and run["run_id"] for run in out["runs"])
    assert apify.gets == [], "the request must not wait for the actor"
    assert [run["input"]["location"] for run in apify.started] == ["Singapore", "Dubai"]
    with SessionLocal() as db:
        saved = db.get(SearchRun, out["runs"][0]["id"])
        assert saved.adapter == "multi_board" and saved.hunter_status == "PENDING"
        assert saved.roles == ["Engineering Manager"] and saved.sites == ["linkedin", "indeed", "glassdoor"]
        assert saved.search_params["hours_old"] == 72 and saved.completed_at is None


def test_more_than_five_roles_are_split_into_batches_of_five(apify):
    roles = [f"Role {n}" for n in range(1, 8)]
    out = start(roles=roles, locations=["Singapore"])

    assert [len(run["roles"]) for run in out["runs"]] == [5, 2]
    assert [run["input"]["searchTerms"] for run in apify.started] == [roles[:5], roles[5:]]
    assert apify.started[0]["input"]["searchTerm"] == "Role 1"


def test_a_run_reports_running_then_succeeded_while_it_works(apify):
    out = start(roles=["Engineering Manager"])
    run_id = out["runs"][0]["run_id"]
    apify.runs[run_id]["status"] = "RUNNING"

    sync()
    assert run_detail(out["runs"][0]["id"])["status"] == "RUNNING"
    assert not run_detail(out["runs"][0]["id"])["completed"]

    apify.finish(run_id, [])
    sync()
    finished = run_detail(out["runs"][0]["id"])
    assert finished["status"] == "SUCCEEDED" and finished["completed"] and finished["result_count"] == 0
    assert finished["import"]["created"] == 0 and finished["import"]["empty"] is True


@pytest.mark.parametrize("payload, expected", [
    ({"roles": []}, "roles"), ({"roles": ["   "]}, "roles"), ({"locations": []}, "locations"),
    ({"sites": ["monster"]}, "sites"), ({"max_results": 0}, "max_results"), ({"hours_old": 0}, "hours_old"),
    ({"job_type": "freelance"}, "job_type"),
])
def test_searches_are_validated_before_any_actor_runs(apify, payload, expected):
    response = client.post("/api/hunter/search", json=body(**payload))
    assert response.status_code == 422
    assert expected in response.text
    assert apify.started == []


def test_a_search_without_an_apify_key_explains_what_to_do(apify):
    with SessionLocal() as db:
        set_user_keys(db, {"apify": ""}, user_id=ensure_test_user().id)
    try:
        response = client.post("/api/hunter/search", json=body())
        assert response.status_code == 400 and "Apify API token" in response.json()["detail"]
        assert apify.started == []
    finally:
        with SessionLocal() as db:
            set_user_keys(db, {"apify": TOKEN}, user_id=ensure_test_user().id)


# ───────────── actor inputs ─────────────

def test_multi_board_input_matches_the_actors_schema():
    runs, _ = inputs({"locations": ["Singapore"], "job_type": "fulltime"})

    assert len(runs) == 1
    assert runs[0].input == {
        "searchTerm": "Engineering Manager", "searchTerms": ["Engineering Manager", "Head of Engineering"],
        "location": "Singapore", "sites": ["linkedin", "indeed", "glassdoor"], "maxResults": 20, "hoursOld": 72,
        "isRemote": False, "countryIndeed": "singapore", "distance": 50, "linkedinFetchDescription": True,
        "enforceAnnualSalary": True, "jobType": "fulltime"}


def test_linkedin_fallback_input_matches_the_actors_schema():
    runs, _ = inputs({"mode": "fallback", "sites": ["linkedin"]})
    assert runs[0].adapter == "linkedin"
    assert runs[0].input == {"queries": ["Engineering Manager", "Head of Engineering"], "location": "Singapore",
                             "country": "singapore", "maxResultsPerQuery": 20, "datePosted": "pastWeek"}

    day, _ = inputs({"mode": "fallback", "sites": ["linkedin"], "hours_old": 24})
    assert day[0].input["datePosted"] == "past24h"
    long_window, warnings = inputs({"mode": "fallback", "sites": ["linkedin"], "hours_old": 2000})
    assert "datePosted" not in long_window[0].input
    assert any("30 days" in warning for warning in warnings)


def test_indeed_fallback_runs_one_position_at_a_time():
    runs, warnings = inputs({"mode": "fallback", "sites": ["indeed"], "locations": ["Singapore", "Dubai"]})

    assert len(runs) == 4  # one run per role and location
    assert runs[0].input == {"position": "Engineering Manager", "location": "Singapore", "country": "SG",
                             "maxItemsPerSearch": 20, "parseCompanyDetails": False, "saveOnlyUniqueItems": True,
                             "followApplyRedirects": True}
    assert [run.input["country"] for run in runs] == ["SG", "SG", "AE", "AE"]
    assert any("no date filter" in warning for warning in warnings)


def test_glassdoor_fallback_searches_by_keyword_and_location():
    runs, _ = inputs({"mode": "fallback", "sites": ["glassdoor"]})
    assert runs[0].input == {"keyword": "Engineering Manager", "location": "Singapore", "maxItems": 20}
    assert "companyUrl" not in runs[0].input  # company searches only


def test_naukri_fallback_uses_its_own_filters():
    runs, _ = inputs({"mode": "fallback", "sites": ["naukri"], "locations": ["Bangalore, India"], "hours_old": 72})
    assert runs[0].input == {"keyword": "Engineering Manager", "location": "bangalore", "maxItems": 20,
                             "jobAge": "3", "sortBy": "date"}

    contract, _ = inputs({"mode": "fallback", "sites": ["naukri"], "locations": ["Bangalore"],
                          "job_type": "contract", "remote_only": True})
    assert contract[0].input["jobType"] == "contractual" and contract[0].input["workMode"] == "remote"


def test_fallback_mode_picks_an_actor_per_site_and_keeps_the_rest_on_the_primary_actor():
    runs, warnings = inputs({"mode": "fallback", "sites": list(ACTOR_SITES) if (ACTOR_SITES := (
        "linkedin", "indeed", "glassdoor", "naukri", "zip_recruiter", "bayt")) else []})

    by_adapter = {run.adapter: run for run in runs}
    assert set(by_adapter) == {"linkedin", "indeed", "glassdoor", "naukri", "multi_board"}
    assert by_adapter["multi_board"].input["sites"] == ["zip_recruiter", "bayt"]
    assert any("no fallback actor" in warning for warning in warnings)


def test_no_adapter_sends_a_field_the_actor_does_not_accept():
    for mode in ("primary", "fallback"):
        runs, _ = inputs({"mode": mode, "sites": ["linkedin", "indeed", "glassdoor", "naukri"],
                          "locations": ["Bangalore, India"], "job_type": "contract", "remote_only": True,
                          "country": "India", "hours_old": 48})
        for run in runs:
            assert set(run.input) <= ACTOR_SCHEMAS[run.actor_id], run.actor_id


def test_an_adapter_refuses_to_send_an_unsupported_field():
    class BrokenGlassdoor(GlassdoorAdapter):
        def build_input(self, search, roles, location, sites):
            return {"keyword": roles[0], "searchTerms": roles}

    with pytest.raises(AdapterError, match="searchTerms"):
        BrokenGlassdoor().payload(HunterSearch(roles=("Lead",), locations=("Singapore",)), ["Lead"], "Singapore", [])


def test_actor_ids_come_from_settings(monkeypatch):
    monkeypatch.setattr(get_settings(), "apify_linkedin_actor", "someone/other-linkedin", raising=False)
    runs, _ = inputs({"mode": "fallback", "sites": ["linkedin"]})
    assert runs[0].actor_id == "someone/other-linkedin"


# ───────────── posting dates ─────────────

@pytest.mark.parametrize("value, expected, precision", [
    ("2026-09-14", datetime(2026, 9, 14), "date"),
    ("2026-09-14T08:30:00Z", datetime(2026, 9, 14, 8, 30), "datetime"),
    ("2026-09-14T10:30:00+02:00", datetime(2026, 9, 14, 8, 30), "datetime"),
    ("Sep 14, 2026", datetime(2026, 9, 14), "date"),
    ("2 hours ago", datetime(2026, 9, 16, 10, 0), "datetime"),
    ("Posted 1 day ago", datetime(2026, 9, 15), "date"),
    ("3 days ago", datetime(2026, 9, 13), "date"),
    ("an hour ago", datetime(2026, 9, 16, 11, 0), "datetime"),
    ("just now", REFERENCE, "datetime"),
    ("today", datetime(2026, 9, 16), "date"),
    ("yesterday", datetime(2026, 9, 15), "date"),
])
def test_posting_dates_are_normalized(value, expected, precision):
    posted = parse_posted_date(value, REFERENCE)
    assert posted.at == expected and posted.precision == precision and posted.raw == value


@pytest.mark.parametrize("value", ["30+ days ago", "2 months ago", "a year ago", "sometime soon", "n/a"])
def test_unclear_posting_dates_stay_unknown_but_keep_the_original(value):
    posted = parse_posted_date(value, REFERENCE)
    assert posted.at is None and posted.raw == value


def test_missing_and_relative_dates_without_a_scrape_time_are_unknown():
    assert parse_posted_date(None, REFERENCE).at is None and parse_posted_date(None, REFERENCE).raw is None
    assert parse_posted_date("", REFERENCE).raw is None
    assert parse_posted_date("3 days ago", None).at is None  # nothing to count back from
    epoch_ms = int(datetime(2026, 9, 14, 8, 0, tzinfo=UTC).timestamp() * 1000)
    assert parse_posted_date(epoch_ms, None).at == datetime(2026, 9, 14, 8, 0)


# ───────────── importing results ─────────────

def test_results_are_normalized_and_queued_for_ranking(apify):
    t = tag()
    out = start(roles=["Engineering Manager"], locations=["Singapore"])
    number = digits()
    full = board_item(t, site="indeed", id=f"in-{number}", job_url=f"https://sg.indeed.com/viewjob?jk={number}",
                      job_url_direct=f"https://careers.example/{t}?utm_source=indeed", is_remote="False",
                      job_type="fulltime", job_level="Director", salary_min="120000", salary_max="150000",
                      salary_currency="SGD", salary_interval="yearly", salary_source="direct_data",
                      skills="Python, Leadership", experience_range="8-12 Yrs", matched_search_term="engineering manager",
                      new_field_from_the_actor={"kept": True})
    undated = board_item(t, site="indeed", title=f"Staff Engineer {t}")
    undated.pop("date_posted")
    apify.finish(out["runs"][0]["run_id"], [full, undated])

    assert sync()["created"] == 2
    detail = run_detail(out["runs"][0]["id"])
    assert detail["status"] == "SUCCEEDED" and detail["result_count"] == 2 and detail["finished_at"]
    assert detail["import"]["created"] == 2 and detail["import"]["missing_posted_date"] == 1

    job, other = jobs_for(f"Acme {t}")
    assert job.posted_at == datetime(2026, 9, 14) and job.posted_date_raw == "2026-09-14"
    assert job.scraped_at == datetime(2026, 9, 16, 8, 3) and job.discovered_at != job.posted_at
    assert job.source == "apify:indeed" and job.source_board == "indeed" and job.source_job_id == number
    assert job.url.startswith("https://careers.example/") and job.source_url.endswith(f"jk={number}")
    assert job.canonical_apply_url == f"https://careers.example/{t}"  # tracking parameters dropped
    assert (job.salary_min, job.salary_max, job.salary_currency) == (120000.0, 150000.0, "SGD")
    assert job.salary_interval == "yearly" and job.salary_source == "direct_data"
    assert job.skills == ["Python", "Leadership"] and job.experience == "8-12 Yrs" and job.seniority == "Director"
    assert job.job_type == "fulltime" and job.remote_type is None
    assert job.matched_search_role == "Engineering Manager" and job.matched_search_location == "Singapore"
    assert job.apify_actor_id == "openclawai/job-board-scraper" and job.apify_run_id == out["runs"][0]["run_id"]
    assert job.raw["new_field_from_the_actor"] == {"kept": True}  # unknown fields are kept, not dropped
    assert other.posted_at is None and other.posted_date_raw is None

    with SessionLocal() as db:
        states = db.scalars(select(WorkflowTransition.to_state).where(WorkflowTransition.job_id == job.id)
                            .order_by(WorkflowTransition.id)).all()
    assert states == [S.NORMALIZED, S.FILTERED] and job.state is S.FILTERED


def test_relative_dates_count_back_from_the_scrape_and_stale_postings_are_dropped(apify):
    t = tag()
    out = start(mode="fallback", sites=["indeed"], roles=["Engineering Manager"], hours_old=72)
    fresh = {"id": digits(), "positionName": f"Engineering Manager {t}", "company": f"Beta {t}",
             "location": "Singapore", "url": f"https://sg.indeed.com/viewjob?jk={digits()}", "postedAt": "3 days ago",
             "scrapedAt": "2026-09-15T10:00:00Z", "salary": "$9,000 - $12,000 a month",
             "externalApplyLink": "https://apply.example/beta"}
    stale = {**fresh, "id": digits(), "positionName": f"Old Manager {t}", "postedAt": "10 days ago",
             "url": f"https://sg.indeed.com/viewjob?jk={digits()}",
             "externalApplyLink": f"https://apply.example/beta-old-{t}"}
    unknown = {**fresh, "id": digits(), "positionName": f"Undated Manager {t}", "postedAt": "30+ days ago",
               "url": f"https://sg.indeed.com/viewjob?jk={digits()}",
               "externalApplyLink": f"https://apply.example/beta-undated-{t}"}
    apify.finish(out["runs"][0]["run_id"], [fresh, stale, unknown])
    sync()

    detail = run_detail(out["runs"][0]["id"])
    assert detail["import"]["created"] == 2 and detail["import"]["too_old"] == 1
    saved = {job.title: job for job in jobs_for(f"Beta {t}")}
    assert saved[f"Engineering Manager {t}"].posted_at == datetime(2026, 9, 12)
    assert saved[f"Engineering Manager {t}"].posted_date_raw == "3 days ago"
    assert saved[f"Engineering Manager {t}"].salary_min == 9000.0
    assert saved[f"Engineering Manager {t}"].salary_interval == "monthly"
    assert saved[f"Undated Manager {t}"].posted_at is None  # an unknown date is never treated as too old


def test_the_same_posting_from_several_roles_and_runs_is_saved_once(apify):
    t = tag()
    out = start(roles=["Engineering Manager", "Head of Engineering"], locations=["Singapore"])
    item = board_item(t)
    twice = [item, {**item, "matched_search_term": "Head of Engineering"}]
    apify.finish(out["runs"][0]["run_id"], twice)
    sync()

    again = start(roles=["Engineering Manager"], locations=["Singapore"])
    apify.finish(again["runs"][0]["run_id"], [item])
    sync()

    jobs = jobs_for(f"Acme {t}")
    assert len(jobs) == 1
    assert run_detail(again["runs"][0]["id"])["import"] == {**run_detail(again["runs"][0]["id"])["import"],
                                                            "created": 0, "duplicates": 1}
    roles = {entry["role"] for entry in jobs[0].discoveries}
    assert roles == {"Engineering Manager", "Head of Engineering"}
    assert len({entry["apify_run_id"] for entry in jobs[0].discoveries}) == 2


def test_the_same_posting_on_several_boards_is_saved_once(apify):
    t = tag()
    number = digits()
    primary = start(roles=["Engineering Manager"], locations=["Singapore"], sites=["linkedin"])
    without_date = board_item(t, id=f"li-{number}", job_url=f"https://www.linkedin.com/jobs/view/{number}")
    without_date.pop("date_posted")
    apify.finish(primary["runs"][0]["run_id"], [without_date])
    sync()

    fallback = start(mode="fallback", sites=["linkedin", "glassdoor"], roles=["Engineering Manager"],
                     locations=["Singapore"])
    by_adapter = {run["adapter"]: run for run in fallback["runs"]}
    apify.finish(by_adapter["linkedin"]["run_id"], [{
        "job_id": None, "title": f"Engineering Manager {t}", "company_name": f"Acme {t}", "location": "Singapore",
        "job_url": f"https://www.linkedin.com/jobs/view/engineering-manager-at-acme-{number}?trk=public_jobs",
        "posted_at": "2026-09-14", "skills": ["Python"]}])
    apify.finish(by_adapter["glassdoor"]["run_id"], [{
        "jobListingId": int(digits()), "jobTitle": f"Engineering Manager {t}", "companyName": f"Acme {t} Pte Ltd",
        "location": "Singapore", "jobUrl": f"https://www.glassdoor.sg/job-listing/{t}", "ageInDays": 2,
        "salary_min": 100000, "salary_max": 140000, "salaryCurrency": "SGD", "salaryPeriod": "ANNUAL"}])
    sync()

    jobs = jobs_for(f"Acme {t}")
    assert len(jobs) == 1, "the same LinkedIn job ID and the same company, title and city are one job"
    assert {entry["board"] for entry in jobs[0].discoveries} == {"linkedin", "glassdoor"}
    assert jobs[0].posted_at == datetime(2026, 9, 14), "a later discovery filled in the missing posting date"
    assert jobs[0].salary_min == 100000.0 and jobs[0].salary_currency == "SGD"


def test_results_without_a_title_or_company_are_skipped_and_counted(apify):
    t = tag()
    out = start(roles=["Engineering Manager"], locations=["Singapore"])
    good = board_item(t)
    apify.finish(out["runs"][0]["run_id"], [good, {**board_item(t), "title": ""}, {**board_item(t), "company": None}])
    sync()

    assert run_detail(out["runs"][0]["id"])["import"]["missing_title"] == 1
    assert run_detail(out["runs"][0]["id"])["import"]["missing_company"] == 1
    assert len(jobs_for(f"Acme {t}")) == 1


# ───────────── failures ─────────────

def test_a_run_that_cannot_start_does_not_stop_the_others(apify):
    def refuse(actor_input):
        if actor_input["location"] == "Dubai":
            request = httpx.Request("POST", "https://api.apify.com/v2/acts/x/runs")
            response = httpx.Response(400, json={"error": {"message": "Input is not valid: sites must be an array"}},
                                      request=request)
            return httpx.HTTPStatusError("bad input", request=request, response=response)
        if actor_input["location"] == "Riyadh":
            return httpx.ReadTimeout("timed out")
        return None

    apify.start_error = refuse
    out = start(locations=["Singapore", "Dubai", "Riyadh"], roles=["Engineering Manager"])

    assert out["started"] == 1 and out["failed"] == 2
    statuses = {run["location"]: run for run in out["runs"]}
    assert statuses["Dubai"]["status"] == "FAILED" and "Input is not valid" in statuses["Dubai"]["error"]
    assert "did not answer" in statuses["Riyadh"]["error"] and statuses["Riyadh"]["run_id"] is None
    assert statuses["Dubai"]["completed"], "a run that never started needs no syncing"
    sync()
    assert f"/actor-runs/{statuses['Singapore']['run_id']}" in apify.gets
    assert not [path for path in apify.gets if "not-started" in path], "nothing to ask Apify about"


def test_a_failed_actor_run_keeps_partial_results_and_the_fallback_actors_take_over(apify):
    t = tag()
    out = start(roles=["Engineering Manager"], locations=["Singapore"], sites=["linkedin", "bayt"])
    apify.finish(out["runs"][0]["run_id"], [board_item(t)], status="FAILED", message="Actor crashed")
    sync()

    detail = run_detail(out["runs"][0]["id"])
    assert detail["status"] == "FAILED" and "Actor crashed" in detail["error"]
    assert detail["import"]["created"] == 1, "whatever the run collected is still imported"

    fallbacks = client.get("/api/hunter/runs").json()["runs"]
    started = [run for run in fallbacks if run["fallback_for_run_id"] == out["runs"][0]["id"]]
    assert [run["adapter"] for run in started] == ["linkedin"], "Bayt has no fallback actor"
    assert detail["import"]["fallback_run_ids"] == [run["id"] for run in started]

    apify.finish(started[0]["run_id"], [], status="FAILED")
    sync()
    assert not [run for run in client.get("/api/hunter/runs").json()["runs"]
                if run["fallback_for_run_id"] == started[0]["id"]], "a fallback does not start its own fallback"


def test_a_timed_out_run_is_recorded_without_a_fallback_when_it_is_switched_off(apify, monkeypatch):
    monkeypatch.setattr(get_settings(), "apify_auto_fallback", False, raising=False)
    out = start(roles=["Engineering Manager"], locations=["Singapore"], sites=["linkedin"])
    apify.finish(out["runs"][0]["run_id"], [], status="TIMED-OUT")
    sync()

    detail = run_detail(out["runs"][0]["id"])
    assert detail["status"] == "FAILED" and "timed out" in detail["error"]
    assert not [run for run in client.get("/api/hunter/runs").json()["runs"]
                if run["fallback_for_run_id"] == out["runs"][0]["id"]]


@pytest.mark.parametrize("items, problem", [
    ([{"name": "Engineer", "employer": "Acme"}, {"name": "Manager", "employer": "Beta"}], "a title"),
    ({"unexpected": "shape"}, "not a list"),
])
def test_output_that_no_longer_matches_the_adapter_is_marked_for_investigation(apify, monkeypatch, items, problem):
    monkeypatch.setattr(get_settings(), "apify_auto_fallback", False, raising=False)
    out = start(roles=["Engineering Manager"], locations=["Singapore"], sites=["bayt"])
    apify.finish(out["runs"][0]["run_id"], items)
    sync()

    detail = run_detail(out["runs"][0]["id"])
    assert detail["status"] == "NEEDS_INVESTIGATION" and problem in detail["error"]
    assert detail["created_jobs"] == 0, "nothing is imported from output we no longer understand"


# ───────────── search profiles and the scheduler ─────────────

def profile_body(name, **overrides):
    return {**body(roles=["Engineering Manager"], locations=["Singapore"]), "name": name,
            "run_every_minutes": 60, **overrides}


def test_search_profiles_are_saved_and_run_by_the_scheduler(apify, monkeypatch):
    name = f"Singapore leadership {tag()}"
    created = client.post("/api/hunter/profiles", json=profile_body(name))
    assert created.status_code == 201, created.text
    profile_id = created.json()["id"]
    assert client.post("/api/hunter/profiles", json=profile_body(name)).status_code == 409

    off = client.post("/api/hunter/profiles", json=profile_body(f"Paused {tag()}", enabled=False)).json()
    updated = client.put(f"/api/hunter/profiles/{profile_id}", json=profile_body(name, locations=["Singapore", "Dubai"]))
    assert updated.json()["locations"] == ["Singapore", "Dubai"]
    assert profile_id in [p["id"] for p in client.get("/api/hunter/profiles").json()["profiles"]]

    # The scheduler starts due profiles; the Discovery screen's own saved search is not what is tested here.
    monkeypatch.setattr(sj, "_saved_search", lambda ctx, db: "schedule is manual")
    now = datetime.now(UTC).replace(tzinfo=None)
    context = sj.JobContext(sessions=SessionLocal, trigger="schedule", now=now, worker_id="test", interval_minutes=240)
    result = sj.hunter(context)

    assert result["started"] >= 2 and not result["errors"]
    with SessionLocal() as db:
        runs = db.scalars(select(SearchRun).where(SearchRun.search_profile_id == profile_id)).all()
        assert [run.location for run in runs] == ["Singapore", "Dubai"]
        assert not db.scalars(select(SearchRun).where(SearchRun.search_profile_id == off["id"])).all()
        saved = db.get(SearchProfile, profile_id)
        assert saved.last_run_at is not None and saved.next_run_at > now

    before = len(apify.started)
    sj.hunter(context)
    assert len(apify.started) == before, "a profile that ran is not due again until its interval passes"

    t = tag()
    apify.finish(runs[0].external_run_id, [board_item(t)])
    monkeypatch.setattr(get_settings(), "apify_sync_batch_size", 500, raising=False)
    sj.apify_sync(context)
    assert jobs_for(f"Acme {t}")[0].search_profile_id == profile_id

    assert client.post(f"/api/hunter/profiles/{profile_id}/run").status_code == 202
    assert client.delete(f"/api/hunter/profiles/{profile_id}").status_code == 204
    assert client.delete(f"/api/hunter/profiles/{off['id']}").status_code == 204
    assert client.get(f"/api/hunter/profiles/{profile_id}").status_code == 404


def test_the_actor_catalog_is_published_for_the_dashboard(apify):
    catalog = client.get("/api/hunter/actors").json()
    assert {adapter["key"] for adapter in catalog["adapters"]} == set(ADAPTERS)
    multi = next(a for a in catalog["adapters"] if a["key"] == "multi_board")
    assert multi["roles_per_run"] == 5 and multi["recency_filter"] == "exact"
    assert "NEEDS_INVESTIGATION" in catalog["statuses"]
