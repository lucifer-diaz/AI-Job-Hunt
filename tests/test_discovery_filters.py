"""Discovery table filters (/api/jobs/search): date ranges (within, older than, between, no posting date), fit range,
work mode, job type, salary, search setup and the facets the filter bar offers."""
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.main import app
from app.models import Job, SavedSearch, SearchRun
from app.workspace import utcnow
from tests.conftest import ensure_test_user

client = TestClient(app)

POSTING = ("Lead Python platform teams building services on Kubernetes and AWS. You will own the roadmap for our data "
           "platform, mentor five engineers, and work with product to ship reliably.")


@pytest.fixture
def tag():
    return uuid.uuid4().hex[:8]


def add(tag: str, name: str, **fields) -> int:
    response = client.post("/api/jobs/from-url", json={
        "url": f"https://example.com/jobs/{tag}-{name}", "title": f"Engineer {name}", "company": f"Filterco {tag}",
        "location": "Remote", "description": POSTING})
    assert response.status_code == 200, response.text
    job_id = response.json()["job"]["id"]
    if fields:
        with SessionLocal() as db:
            db.info["user_id"] = ensure_test_user().id
            job = db.get(Job, job_id)
            for key, value in fields.items():
                setattr(job, key, value)
            db.commit()
    return job_id


def names(tag: str, **params) -> set[str]:
    response = client.get("/api/jobs/search", params={"q": tag, "per_page": 200, **params})
    assert response.status_code == 200, response.text
    return {item["title"].removeprefix("Engineer ") for item in response.json()["items"]}


def test_found_date_within_older_than_and_between(tag):
    now = utcnow()
    add(tag, "new", discovered_at=now - timedelta(days=1))
    add(tag, "week", discovered_at=now - timedelta(days=10))
    add(tag, "old", discovered_at=now - timedelta(days=40))
    assert names(tag, within="7d") == {"new"}
    assert names(tag, older_than="7d") == {"week", "old"}
    assert names(tag, older_than="30d") == {"old"}
    # Both days are included: date_to covers the whole of its day.
    day = (now - timedelta(days=10)).date().isoformat()
    assert names(tag, date_from=day, date_to=day) == {"week"}
    assert names(tag, date_from=(now - timedelta(days=12)).date().isoformat()) == {"new", "week"}
    assert names(tag, date_to=(now - timedelta(days=30)).date().isoformat()) == {"old"}


def test_posted_dates_never_guess_an_unknown_date(tag):
    now = utcnow()
    add(tag, "dated", posted_at=now - timedelta(days=20))
    add(tag, "undated", posted_at=None)
    assert names(tag, date_field="posted", older_than="7d") == {"dated"}
    assert names(tag, date_field="posted", within="90d") == {"dated"}
    assert names(tag, date_field="posted", posted_unknown=True) == {"undated"}
    assert names(tag, posted_unknown=True) == {"dated", "undated"}, "only meaningful for the posting date"


def test_bad_date_filters_are_refused():
    assert client.get("/api/jobs/search", params={"older_than": "5y"}).status_code == 422
    assert client.get("/api/jobs/search", params={"date_from": "10/01/2026"}).status_code == 422
    assert client.get("/api/jobs/search", params={"date_from": "2026-10-09", "date_to": "2026-10-01"}).status_code == 422
    assert client.get("/api/jobs/search", params={"work_mode": "office"}).status_code == 422


def test_fit_range_work_mode_job_type_and_salary(tag):
    add(tag, "remote", fit_score=85.0, remote_type="remote", job_type="Full-time",
        raw={"salary_min": 150000, "salary_max": 180000, "currency": "USD"})
    add(tag, "hybrid", fit_score=60.0, remote_type="hybrid", job_type="contract")
    add(tag, "unstated", fit_score=None, remote_type=None)
    assert names(tag, min_fit=50, max_fit=69) == {"hybrid"}
    assert names(tag, max_fit=90) == {"remote", "hybrid"}, "an unranked job has no fit to cap"
    assert names(tag, work_mode="remote") == {"remote"}
    assert names(tag, work_mode="onsite_unknown") == {"unstated"}
    assert names(tag, job_type="full-time") == {"remote"}
    assert names(tag, has_salary=True) == {"remote"}
    # A job the compensation check looked at and found no salary in keeps an envelope; that is not a salary.
    add(tag, "checked", compensation={"status": "not_listed", "posting": None, "glassdoor": {"status": "not_configured"}})
    add(tag, "enriched", compensation={"status": "found", "posting": {"min": 90000, "max": None, "currency": "EUR"}})
    assert names(tag, has_salary=True) == {"remote", "enriched"}
    facets = client.get("/api/jobs/search", params={"q": tag}).json()
    assert {"full-time", "contract"} <= set(facets["job_types"])
    assert facets["work_modes"]["remote"] >= 1 and facets["work_modes"]["onsite_unknown"] >= 1


def test_search_setup_and_keywords(tag):
    user = ensure_test_user().id
    with SessionLocal() as db:
        db.info["user_id"] = user
        setup = SavedSearch(user_id=user, name=f"Setup {tag}", settings={})
        db.add(setup)
        db.flush()
        run = SearchRun(user_id=user, provider="apify", saved_search_id=setup.id, external_run_id=uuid.uuid4().hex)
        db.add(run)
        db.commit()
        setup_id, run_id = setup.id, run.id
    add(tag, "found", search_run_id=run_id)
    add(tag, "manual")
    assert names(tag, setup_id=setup_id) == {"found"}
    assert [setup_id, f"Setup {tag}"] in client.get("/api/jobs/search", params={"q": tag}).json()["setups"]
    assert names(tag, keywords="kubernetes,roadmap") == {"found", "manual"}
    assert names(tag, keywords="kubernetes,rust/golang") == set()


def test_filters_combine_with_and(tag):
    now = utcnow()
    add(tag, "a", discovered_at=now - timedelta(days=20), remote_type="remote")
    add(tag, "b", discovered_at=now - timedelta(days=20), remote_type="hybrid")
    add(tag, "c", discovered_at=now - timedelta(days=1), remote_type="remote")
    assert names(tag, older_than="7d", work_mode="remote") == {"a"}
