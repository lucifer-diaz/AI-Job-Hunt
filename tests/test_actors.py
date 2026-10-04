import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.actors import item_normalizer, plan_runs, waas_family
from app.services.sources import build_apify_input, normalize_apify_job

client = TestClient(app)

BASE = {"roles": ["Lead AI Engineer", "Engineering Manager"], "locations": ["New York, NY", "Remote"],
        "max_results": 25, "hours_old": 72, "job_type": "fulltime", "seniority": ["senior", "director"],
        "sites": ["linkedin", "indeed"], "country_indeed": "India", "distance": 25, "is_remote": False,
        "easy_apply": False, "fetch_description": True, "enforce_annual_salary": True,
        "residential_proxy": False, "salary_only": True, "exclude_keywords": ["intern"],
        "remote_platforms": ["remotive", "himalayas"], "skip_agencies": True}


STARTUP = ["antishock/workatastartup-jobs-scraper", "automation-lab/ycombinator-jobs-scraper",
           "parseforge/hn-whoishiring-scraper", "blackfalcondata/wellfound-scraper"]


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
    # "Results per search" is the run's total: 25 over 2 boards × 2 roles asks each for 7.
    assert data["maxResults"] == 7 and data["hoursOld"] == 72 and data["jobType"] == "fulltime"
    assert runs[0]["search_params"]["max_jobs"] == 25
    # The location names the country Indeed searches; the setting covers a location without one.
    assert data["countryIndeed"] == "usa" and runs[1]["input"]["countryIndeed"] == "india"
    assert any("Indeed and Glassdoor search usa for 'New York, NY'" in w for w in warnings)
    assert data["distance"] == 25
    assert data["linkedinFetchDescription"] is True and data["enforceAnnualSalary"] is True
    assert data["proxyConfiguration"] == {"useApifyProxy": True, "apifyProxyGroups": []}
    assert any("Indeed ignores job type" in w for w in warnings)


def test_multi_board_country_comes_from_the_location():
    runs, _ = inputs_for("openclawai/job-board-scraper", locations=["Bengaluru, India"], country_indeed="usa")
    assert runs[0]["input"]["countryIndeed"] == "india" and runs[0]["input"]["location"] == "Bengaluru, India"


def test_multi_board_splits_the_limit_and_warns_when_it_cannot():
    runs, warnings = inputs_for("openclawai/job-board-scraper", roles=["Backend Engineer"], locations=[],
                                sites=["linkedin", "indeed", "glassdoor"], max_results=30)
    assert runs[0]["input"]["maxResults"] == 10 and not any("split evenly" in w for w in warnings)
    runs, warnings = inputs_for("openclawai/job-board-scraper", roles=["Backend Engineer"], locations=[],
                                sites=["linkedin", "indeed", "glassdoor"], max_results=5)
    assert runs[0]["input"]["maxResults"] == 2 and any("the import stops at 5" in w for w in warnings)


def test_multi_board_adds_the_primary_skill_to_each_search_term():
    runs, _ = inputs_for("openclawai/job-board-scraper", roles=["Backend Engineer", "Python Developer"],
                         primary_skills=[" Python ", "Django", "python"], locations=["Bengaluru, India"])
    data = runs[0]["input"]
    assert data["searchTerm"] == "Backend Engineer Python"
    assert data["searchTerms"] == ["Backend Engineer Python", "Python Developer"]  # never "Python Developer Python"
    assert runs[0]["search_params"]["primary_skills"] == ["Python", "Django"]
    assert runs[0]["roles"] == ["Backend Engineer", "Python Developer"]  # the scope keeps the plain roles


def test_other_actors_carry_scope_but_no_import_cap():
    runs, _ = inputs_for("crawlerbros/naukri-scraper", primary_skills=["Java"])
    assert runs[0]["search_params"]["primary_skills"] == ["Java"] and "max_jobs" not in runs[0]["search_params"]


def test_custom_actor_skill_placeholders():
    custom = {"id": "me/board", "fan_out": "role", "input_template": {"q": "{{role}} {{skill}}", "tags": "{{skills}}"}}
    runs, warnings = plan_runs({**BASE, "actors": ["me/board"], "custom_actors": [custom],
                                "primary_skills": ["Go", "Rust"]})
    assert runs[0]["input"] == {"q": "Lead AI Engineer Go", "tags": ["Go", "Rust"]} and not warnings


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
    assert len([a for a in catalog["actors"] if not a["custom"]]) == 9
    assert "Startups" in catalog["groups"]
    assert {a["group"] for a in catalog["actors"] if a["id"] in STARTUP} == {"Startups"}


def test_work_at_a_startup_searches_role_families_once():
    runs, warnings = inputs_for("antishock/workatastartup-jobs-scraper", roles=["Lead AI Engineer", "Product Designer"],
                                yc_batches=["w24", "S23", "bogus"], is_remote=True)
    assert len(runs) == 1
    assert runs[0]["input"] == {"roles": ["eng", "design"], "remote": True, "maxResults": 25,
                                "includeCompanyDetails": True, "ycBatches": ["W24", "S23"]}
    assert any("cannot filter by city" in w for w in warnings)
    # A role with no family searches every family rather than silently dropping it.
    runs, warnings = inputs_for("antishock/workatastartup-jobs-scraper", roles=["Chief Vibes Officer"], locations=["Remote"])
    assert runs[0]["input"]["roles"] == [] and runs[0]["input"]["remote"] is True
    assert any("no role family matches" in w for w in warnings)
    assert waas_family("Staff ML Engineer") == "eng" and waas_family("Head of Growth") == "marketing"


def test_y_combinator_jobs_runs_per_role_with_schema_names():
    runs, warnings = inputs_for("automation-lab/ycombinator-jobs-scraper", yc_batches=["W24", "S23"], startup_details=False)
    assert [r["input"]["query"] for r in runs] == BASE["roles"]
    data = runs[0]["input"]
    assert data == {"query": "Lead AI Engineer", "maxItems": 25, "remoteOnly": False, "includeIncomplete": False,
                    "includeDetails": False, "jobType": "Full-time", "companyBatch": "W24"}
    assert any("one batch only" in w for w in warnings)
    _, warnings = inputs_for("automation-lab/ycombinator-jobs-scraper", job_type="contract", locations=["Remote"])
    assert any("full-time and internship" in w for w in warnings)


def test_hn_whos_hiring_month_salary_and_remote():
    runs, _ = inputs_for("parseforge/hn-whoishiring-scraper", locations=["Remote"], hn_month="2026-08", min_salary=150000)
    assert runs[0]["input"] == {"keyword": "Lead AI Engineer", "month": "2026-08", "maxItems": 25,
                                "remoteOnly": True, "minSalary": 150000}
    runs, warnings = inputs_for("parseforge/hn-whoishiring-scraper", hn_month="August")
    assert runs[0]["input"]["month"] == "current" and any("YYYY-MM" in w for w in warnings)


def test_wellfound_maps_every_supported_filter():
    runs, warnings = inputs_for("blackfalcondata/wellfound-scraper", min_salary=120000, visa_sponsorship=True)
    assert len(runs) == 2 and not warnings
    data = runs[0]["input"]
    assert data["query"] == "Lead AI Engineer" and data["location"] == ["new-york"] and data["remote"] is False
    assert data["jobType"] == "fulltime" and data["experienceLevel"] == "senior"  # the lowest seniority chosen
    assert data["salaryMin"] == 120000 and data["visaSponsorship"] is True and data["maxAgeMinutes"] == 72 * 60
    assert data["excludeKeywords"]["keywords"] == ["intern"] and data["skipReposts"] is True
    remote, _ = inputs_for("blackfalcondata/wellfound-scraper", locations=["Remote"], seniority=[])
    assert remote[0]["input"]["remote"] is True and "location" not in remote[0]["input"]
    assert "experienceLevel" not in remote[0]["input"]


def test_startup_items_normalize_to_jobs_with_listing_facts():
    waas = normalize_apify_job(item_normalizer(STARTUP[0])({
        "jobId": 77, "jobTitle": "Backend Engineer", "companyName": "Acme", "location": "San Francisco",
        "jobUrl": "https://www.workatastartup.com/jobs/77", "salary": "$150K - $190K", "equity": "0.2% - 0.5%",
        "ycBatch": "W24", "sponsorsVisa": True}), "workatastartup")
    assert (waas["source"], waas["source_ref"], waas["title"], waas["company"]) == (
        "apify:workatastartup", "workatastartup:77", "Backend Engineer", "Acme")
    assert "Salary: $150K - $190K" in waas["description"] and "Visa sponsorship: yes" in waas["description"]
    remote_sf = normalize_apify_job(item_normalizer(STARTUP[0])({
        "jobId": 78, "jobTitle": "ML Engineer", "companyName": "Acme", "location": "San Francisco", "remote": True,
        "postedAt": "2026-09-20T10:00:00Z"}))
    assert remote_sf["location"] == "Remote · San Francisco"  # a remote role that names a city stays remote
    assert remote_sf["posted_at"].isoformat() == "2026-09-20T10:00:00" and remote_sf["posted_date_raw"] == "2026-09-20T10:00:00Z"
    assert waas["posted_at"] is None  # no date given: unknown, never guessed

    yc = normalize_apify_job(item_normalizer(STARTUP[1])({
        "id": 93174, "title": "Forward Deployed Engineer", "companyName": "Confido", "location": "", "isRemote": True,
        "url": "https://www.ycombinator.com/companies/confido/jobs/X", "descriptionText": "Build integrations.",
        "source": "ycombinator.com", "role": "eng"}))
    # The item's own `source` and `role` fields do not leak into the board or the title.
    assert yc["source"] == "apify:ycombinator" and yc["title"] == "Forward Deployed Engineer"
    assert yc["location"] == "Remote" and yc["description"].startswith("Build integrations.")

    hn = normalize_apify_job(item_normalizer(STARTUP[2])({
        "company": "Tiny Co", "role": "Senior Rust Engineer", "location": "Berlin", "remote": True,
        "salaryRaw": "€90k", "stackText": "Rust, Postgres", "hnCommentUrl": "https://news.ycombinator.com/item?id=1",
        "description": "We build databases."}))
    assert hn["title"] == "Senior Rust Engineer" and hn["url"] == "https://news.ycombinator.com/item?id=1"
    assert hn["source_ref"].startswith("hackernews:") and "Stack: Rust, Postgres" in hn["description"]

    wf = normalize_apify_job(item_normalizer(STARTUP[3])({
        "id": "4716549", "title": "Senior Software Engineer", "companyName": "Hourglass",
        "locationNames": ["New York City", "San Francisco Bay Area"], "remote": False,
        "portalUrl": "https://wellfound.com/jobs/4716549", "description": "Fintech.", "compensation": "$175k – $225k"}))
    assert wf["location"] == "New York City, San Francisco Bay Area" and wf["source_ref"] == "wellfound:4716549"
    assert "Compensation: $175k – $225k" in wf["description"]
    assert item_normalizer("openclawai/job-board-scraper") is None and item_normalizer("someone/else") is None


def test_profile_api_validates_startup_settings():
    bad = client.post("/api/search/profile/preview", json={
        "roles": ["AI Engineer"], "actors": STARTUP, "yc_batches": ["2024"], "hn_month": "May"}).json()
    assert "YC batches" in bad["detail"] and "HN thread month" in bad["detail"]
    ok = client.post("/api/search/profile/preview", json={
        "roles": ["AI Engineer"], "locations": ["Remote"], "actors": STARTUP, "yc_batches": ["W24"],
        "hn_month": "2026-09", "min_salary": 100000}).json()
    assert [r["actor_id"] for r in ok["runs"]] == STARTUP


# ───────────── Naukri ─────────────

NAUKRI = "crawlerbros/naukri-scraper"
INDIA = {"locations": ["Bengaluru, Karnataka, India", "Gurugram"], "roles": ["Product Manager", "Lead Engineer"]}


def test_naukri_runs_once_per_role_and_city_with_schema_names():
    runs, warnings = inputs_for(NAUKRI, **INDIA)
    assert [(r["roles"], r["location"]) for r in runs] == [
        (["Product Manager"], "Bengaluru, Karnataka, India"), (["Product Manager"], "Gurugram"),
        (["Lead Engineer"], "Bengaluru, Karnataka, India"), (["Lead Engineer"], "Gurugram")]
    data = runs[0]["input"]
    assert data == {"keyword": "Product Manager", "location": "bangalore", "maxItems": 25, "sortBy": "date",
                    "jobAge": "3", "jobType": "full-time"}  # 72 hours → the 3-day bucket; Naukri's own city slug
    assert runs[1]["input"]["location"] == "gurgaon" and not warnings


def test_naukri_maps_remote_age_and_warns_outside_india():
    runs, _ = inputs_for(NAUKRI, locations=["Remote"], hours_old=24 * 10, job_type="")
    assert runs[0]["input"] == {"keyword": "Lead AI Engineer", "maxItems": 25, "sortBy": "date", "workMode": "remote",
                                "jobAge": "15"}
    _, old = inputs_for(NAUKRI, **INDIA, hours_old=24 * 60)
    assert any("at most 30 days" in w for w in old)
    _, abroad = inputs_for(NAUKRI, roles=["PM"], locations=["London, UK"])
    assert any("Naukri lists jobs in India" in w for w in abroad)


def test_naukri_items_keep_their_description():
    job = normalize_apify_job(item_normalizer(NAUKRI)({
        "id": "090426506296", "title": "Associate Product Manager", "companyName": "PayU", "location": "Mumbai",
        "jdURL": "/job-listings-associate-product-manager-payu-india-mumbai-1-to-6-years-090426506296",
        "jobDescription": "<p>We are looking for an APM to own <b>checkout</b>.</p><ul><li>Define the roadmap</li></ul>",
        "experience": "1-6 Yrs", "salary": "Not disclosed", "skills": ["Product management", "SQL"]}), "naukri")
    assert (job["source"], job["source_ref"], job["company"]) == ("apify:naukri", "naukri:090426506296", "PayU")
    assert job["url"].startswith("https://www.naukri.com/job-listings-associate-product-manager")
    assert "own checkout" in job["description"] and "Define the roadmap" in job["description"]
    assert "Experience: 1-6 Yrs" in job["description"] and "Skills: Product management, SQL" in job["description"]


def test_naukri_is_not_scraped_twice_without_a_warning():
    both = {**BASE, "sites": ["linkedin", "naukri"], "actors": ["openclawai/job-board-scraper", NAUKRI]}
    _, warnings = plan_runs(both)
    assert any("untick Naukri there" in w for w in warnings)
    _, multi_only = plan_runs({**both, "actors": ["openclawai/job-board-scraper"]})
    assert any("Add the Naukri actor" in w for w in multi_only)
    _, fine = plan_runs({**both, "sites": ["linkedin"]})
    assert not any("untick Naukri" in w or "Add the Naukri actor" in w for w in fine)


def test_the_catalog_says_which_boards_come_with_descriptions():
    actors = {a["id"]: a for a in client.get("/api/search/actors").json()["actors"]}
    assert "Indeed" in actors["openclawai/job-board-scraper"]["summary"]
    assert "without them" in actors["openclawai/job-board-scraper"]["summary"]
    assert actors[NAUKRI]["group"] == "Job boards" and actors[NAUKRI]["fan_out"] == "role_location"
