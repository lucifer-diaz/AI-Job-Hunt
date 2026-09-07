import os
os.environ["LLM_PROVIDER"] = "mock"
os.environ["DATABASE_URL"] = "sqlite:///./test_apify.db"

from app.db import Base, SessionLocal, engine
from app.models import Job
from app.services.sources import build_apify_input, import_apify_items, normalize_apify_job


def setup_function():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def test_build_default_apify_input():
    actor_input = build_apify_input({
        "search_terms": ["Engineering Manager", "Lead AI Engineer"],
        "location": "Singapore",
        "sites": ["linkedin", "indeed"],
        "max_results": 15,
        "hours_old": 72,
        "is_remote": False,
        "linkedin_fetch_description": True,
        "custom_input": {},
    })
    assert actor_input["searchTerms"] == ["Engineering Manager", "Lead AI Engineer"]
    assert actor_input["maxResults"] == 15
    assert actor_input["hoursOld"] == 72
    assert actor_input["linkedinFetchDescription"] is True


def test_normalize_and_dedupe_apify_results():
    item = {
        "id": "in-abc123",
        "title": "Senior AI Engineer",
        "company": "Example Inc",
        "location": "Remote",
        "site": "indeed",
        "job_url": "https://example.test/job/abc123",
        "job_url_direct": "https://example.test/apply/abc123",
        "description": "Build production AI systems.",
    }
    normalized = normalize_apify_job(item)
    assert normalized is not None
    assert normalized["source"] == "apify:indeed"
    assert normalized["url"].endswith("/apply/abc123")

    db = SessionLocal()
    first = import_apify_items(db, [item])
    second = import_apify_items(db, [item])
    assert first["created"] == 1
    assert second["created"] == 0
    assert db.query(Job).count() == 1
    db.close()
