import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.actors import plan_runs
from app.services.sources import build_apify_input, normalize_apify_job

client = TestClient(app)

BASE = {"roles": ["Lead AI Engineer", "Engineering Manager"], "locations": ["New York, NY", "Remote"],
        "max_results": 25, "hours_old": 72, "job_type": "fulltime", "seniority": ["senior", "director"],
        "sites": ["linkedin", "indeed"], "country_indeed": "India", "distance": 25, "is_remote": False,
        "easy_apply": False, "fetch_description": True, "enforce_annual_salary": True,
        "residential_proxy": False, "salary_only": True, "exclude_keywords": ["intern"],
        "remote_platforms": ["remotive", "himalayas"], "skip_agencies": True}


def inputs_for(actor, **overrides):
    runs, warnings = plan_runs({**BASE, "actors": [actor], **overrides})
    return runs, warnings


def test_multi_board_sends_every_filter_with_schema_names():
    runs, warnings = inputs_for("openclawai/job-board-scraper")
    assert [r["location"] for r in runs] == ["New York, NY", "Remote"]  # one run per location
    data = runs[0]["input"]
    assert data["searchTerm"] == "Lead AI Engineer"
    assert data["searchTerms"] == ["Lead AI Engineer", "Engineering Manager"]
    assert data["location"] == "New York, NY" and data["sites"] == ["linkedin", "indeed"]
    assert data["maxResults"] == 25 and data["hoursOld"] == 72 and data["jobType"] == "fulltime"
    assert data["countryIndeed"] == "india" and data["distance"] == 25
    assert data["linkedinFetchDescription"] is True and data["enforceAnnualSalary"] is True
    assert data["proxyConfiguration"] == {"useApifyProxy": True, "apifyProxyGroups": []}
    assert any("Indeed ignores job type" in w for w in warnings)


def test_multi_board_drops_hours_when_easy_apply():
    runs, warnings = inputs_for("openclawai/job-board-scraper", easy_apply=True, locations=[])
    assert len(runs) == 1 and "hoursOld" not in runs[0]["input"] and runs[0]["input"]["easyApply"] is True
    assert any("Easy Apply" in w for w in warnings)


def test_advanced_linkedin_single_run_mapping():
    runs, warnings = inputs_for("fantastic-jobs/advanced-linkedin-job-search-api",
                                locations=["Bengaluru, Karnataka, India", "Remote"])
    assert len(runs) == 1
    data = runs[0]["input"]
    assert data["titleSearch"] == BASE["roles"] and data["timeRange"] == "7d"
    assert data["locationSearch"] == ["Bengaluru, Karnataka, India"]
    assert data["aiEmploymentTypeFilter"] == ["FULL_TIME"]
    assert data["seniorityFilter"] == ["Director", "Mid-Senior level"]
    assert data["hasSalary"] is True and data["removeAgency"] is True and data["titleExclusionSearch"] == ["intern"]
    assert not warnings


def test_remote_aggregators_fan_out_per_role():
    runs, _ = inputs_for("benthepythondev/remote-jobs-aggregator")
    assert [r["input"]["searchKeywords"] for r in runs] == BASE["roles"]
    data = runs[0]["input"]
    assert data["platforms"] == ["remotive", "himalayas"] and data["postedWithinDays"] == 3
    assert data["jobTypes"] == ["full-time"] and data["locationKeywords"] == ["New York, NY"]
    remoteok, _ = inputs_for("shahidirfan/Remoteok-Job-Scraper")
    assert remoteok[0]["input"] == {"startUrl": "https://remoteok.com/remote-jobs", "keyword": "Lead AI Engineer",
                                    "results_wanted": 25, "proxyConfiguration": {"useApifyProxy": False}}


def test_custom_actor_template_substitution_and_limits():
    custom = {"id": "someone/jobs", "name": "Mine", "fan_out": "role_location",
              "input_template": {"query": "{{role}} in {{location}}", "max": "{{max_results}}", "where": "{{locations}}",
                                 "bogus": "{{nope}}"}}
    runs, warnings = plan_runs({**BASE, "actors": ["someone/jobs"], "custom_actors": [custom]})
    assert len(runs) == 4
    assert runs[0]["input"]["query"] == "Lead AI Engineer in New York, NY" and runs[0]["input"]["max"] == 25
    assert runs[0]["input"]["where"] == ["New York, NY"]
    assert any("nope" in w for w in warnings)
    # 5 roles × 5 locations × 2 role_location actors = 50 runs, above the cap of 30.
    with pytest.raises(ValueError, match="limit"):
        plan_runs({**BASE, "roles": ["a", "b", "c", "d", "e"], "locations": ["1", "2", "3", "4", "5"],
                   "custom_actors": [custom, {**custom, "id": "other/jobs"}], "actors": ["someone/jobs", "other/jobs"]})


def test_legacy_input_uses_single_search_term():
    data = build_apify_input({"search_terms": ["software engineer"], "location": "New York, NY",
                              "easy_apply": True, "hours_old": 24, "country_indeed": "usa"})
    assert data["searchTerm"] == "software engineer" and "searchTerms" not in data
    assert data["easyApply"] is True and "hoursOld" not in data and data["countryIndeed"] == "usa"


def test_normalizer_handles_other_actor_shapes():
    linkedin = normalize_apify_job({"id": "9", "title": "ML Lead", "organization": "Acme",
                                    "locations_derived": ["Pune, Maharashtra, India"], "url": "https://x/9",
                                    "description_text": "Build models"}, "linkedin")
    assert linkedin["source"] == "apify:linkedin" and linkedin["company"] == "Acme"
    assert linkedin["location"] == "Pune, Maharashtra, India" and linkedin["description"] == "Build models"


def test_profile_api_validates_and_previews():
    bad = client.put("/api/search/profile", json={"roles": ["AI"], "actors": ["nobody/nothing"]})
    assert bad.status_code == 422 and "Unknown actors" in bad.json()["detail"]
    preview = client.post("/api/search/profile/preview", json={
        "roles": ["AI Engineer"], "locations": ["Remote"],
        "actors": ["openclawai/job-board-scraper", "shahidirfan/Remoteok-Job-Scraper"]}).json()
    assert [r["actor_id"] for r in preview["runs"]] == ["openclawai/job-board-scraper", "shahidirfan/Remoteok-Job-Scraper"]
    catalog = client.get("/api/search/actors").json()
    assert len([a for a in catalog["actors"] if not a["custom"]]) == 4
