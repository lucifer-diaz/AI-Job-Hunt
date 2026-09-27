"""Job description recovery, the deterministic half: is a description usable, what is a fetched page, where is the
posting on it, and may recovered text be saved. No database, no network, no model."""
from types import SimpleNamespace

import pytest

from app.services import jd_extractors as x
from app.services import jd_quality as q
from tests.jd_support import page

REAL = page("posting")
POSTING_TEXT = x.extract(REAL, "https://careers.northwind.test/jobs/1").text


# ───────────── missing-description detection ─────────────

@pytest.mark.parametrize("text", [None, "", "   \n\t "])
def test_missing_descriptions_are_unusable(text):
    assert q.description_problem(text) == q.EMPTY
    assert not q.is_job_description_usable(SimpleNamespace(description=text))


@pytest.mark.parametrize("text", ["View job", "Apply here", "See job posting", "Click to apply", "click here to apply!",
                                  "Apply here: https://jobs.example.com/123", "N/A", "Read more"])
def test_placeholder_descriptions_are_unusable(text):
    assert q.description_problem(text) == q.PLACEHOLDER


def test_a_truncated_description_is_unusable_even_when_it_is_not_tiny():
    cut = "We are looking for a backend engineer to design and build our payments APIs. " * 6 + "…see more"
    assert len(cut) > 400 and q.description_problem(cut) == q.TRUNCATED


def test_site_boilerplate_is_not_a_description():
    nav = "\n".join(["Home", "Jobs", "Sign in", "Privacy policy", "Cookie settings", "Terms of use", "Contact us",
                     "© 2026 Example", "Follow us", "Careers home"] * 8)
    assert q.description_problem(nav) == q.BOILERPLATE


def test_length_alone_does_not_decide():
    padded = "Lorem ipsum dolor sit amet consectetur adipiscing elit. " * 12  # long, but says nothing about a job
    assert len(padded) > 600 and q.description_problem(padded) is not None
    short_but_real = ("About the role: we are looking for a data engineer to join our analytics team.\n"
                      "Responsibilities:\n- Build and maintain batch pipelines\n- Own data quality for core tables\n"
                      "Requirements:\n- 3+ years of experience with Python and SQL\n- Experience with dbt\n"
                      "Nice to have: Airflow. Benefits: remote, learning budget.")
    assert 300 < len(short_but_real) < 500 and q.description_problem(short_but_real) is None
    assert q.description_problem("About the role: data engineer. Requirements: SQL.") == q.TOO_SHORT


def test_a_real_posting_is_usable():
    assert q.description_problem(POSTING_TEXT) is None
    assert q.is_job_description_usable(SimpleNamespace(description=POSTING_TEXT))


def test_the_minimum_is_configurable(monkeypatch):
    from app.settings import get_settings

    text = "We are looking for an engineer. Responsibilities: build services. Requirements: experience with Python."
    monkeypatch.setattr(get_settings(), "jd_description_min_characters", 50)
    assert q.description_problem(text) is None
    monkeypatch.setattr(get_settings(), "jd_description_min_characters", 5000)
    assert q.description_problem(text) == q.TOO_SHORT


def test_text_a_person_saved_is_always_usable():
    assert q.is_job_description_usable(SimpleNamespace(description="Short note", description_source="USER"))


# ───────────── pages that are not postings ─────────────

def test_login_walls_and_challenges_are_classified():
    login = x.extract(page("login"))
    assert q.classify_page(status=200, text=login.page_text, title=login.title,
                           has_password_field=login.has_password_field) == q.LOGIN_REQUIRED
    challenge = x.extract(page("captcha"))
    assert q.classify_page(status=200, text=challenge.page_text, title=challenge.title) == q.CAPTCHA_OR_ANTI_BOT
    assert q.classify_page(status=403, text="Forbidden") == q.ACCESS_DENIED
    assert q.classify_page(status=401, text="") == q.LOGIN_REQUIRED


def test_removed_and_closed_postings_are_classified():
    assert q.classify_page(status=404, text="") == q.JOB_REMOVED
    assert q.classify_page(status=410, text="") == q.JOB_REMOVED
    closed = x.extract(page("closed"))
    assert q.closed_or_error(closed.page_text, closed.title) == q.JOB_CLOSED


def test_rate_limits_and_ordinary_pages_are_not_classified():
    assert q.classify_page(status=429, text="Too many requests") is None
    assert q.classify_page(status=200, text=POSTING_TEXT) is None


# ───────────── validating recovered text ─────────────

def test_a_real_posting_validates():
    result = q.validate_recovered(POSTING_TEXT, job_title="Senior Platform Engineer")
    assert result.ok and result.strong_signals >= 3 and result.useful_ratio > 0.7


@pytest.mark.parametrize("name,reason", [("login", q.LOGIN_REQUIRED), ("captcha", q.CAPTCHA_OR_ANTI_BOT)])
def test_login_and_captcha_text_never_validates(name, reason):
    text = x.extract(page(name)).page_text
    result = q.validate_recovered(text)
    assert not result.ok and reason in result.reasons


def test_an_error_page_never_validates():
    text = "Oops! Something went wrong. Page not found. " * 20
    assert not q.validate_recovered(text).ok


def test_navigation_only_text_never_validates():
    nav = "\n".join(["Home", "Jobs", "Sign in", "Privacy policy", "Cookies", "Terms of use"] * 20)
    assert "MOSTLY_BOILERPLATE" in q.validate_recovered(nav).reasons


def test_a_redirect_to_a_generic_page_is_unrelated():
    result = q.validate_recovered(POSTING_TEXT, job_title="Senior Platform Engineer",
                                  source_url="https://board.test/jobs/123", final_url="https://employer.test/careers")
    assert q.UNRELATED_PAGE in result.reasons
    # Aggregator → the employer's own posting page is normal.
    ok = q.validate_recovered(POSTING_TEXT, job_title="Senior Platform Engineer",
                              source_url="https://board.test/jobs/123",
                              final_url="https://boards.greenhouse.io/northwind/jobs/555")
    assert ok.ok
    # A cross-host landing page that never names the role is not this posting.
    assert q.UNRELATED_PAGE in q.validate_recovered(
        POSTING_TEXT, job_title="Chief Veterinary Officer", source_url="https://board.test/jobs/1",
        final_url="https://other.test/jobs/9").reasons


# ───────────── better description, content hash ─────────────

def test_a_good_description_is_not_replaced_by_a_shorter_or_equal_one():
    assert not q.better_than(POSTING_TEXT[:700], POSTING_TEXT)
    assert not q.better_than(POSTING_TEXT, POSTING_TEXT)
    assert q.better_than(POSTING_TEXT, "View job")
    assert q.better_than(POSTING_TEXT * 2, POSTING_TEXT[: len(POSTING_TEXT) // 2] + " Requirements: Python.")


def test_a_good_description_is_only_replaced_by_a_fuller_version_of_the_same_posting():
    other = x.extract(page("json_ld")).text
    different_and_longer = (other + "\n") * 3
    assert not q.better_than(different_and_longer, POSTING_TEXT), "a different posting never replaces a good one"
    fuller = POSTING_TEXT + "\n\nAbout Northwind\n" + "We build logistics software for retailers worldwide. " * 10
    assert q.better_than(fuller, POSTING_TEXT)


def test_the_content_hash_ignores_formatting():
    html = "<p>We are hiring</p><ul><li>Python</li></ul>"
    assert q.content_hash(html) == q.content_hash("We   are HIRING\n- Python")
    assert q.content_hash("We are hiring Python") != q.content_hash("We are hiring Go")
    assert q.content_hash("") is None and q.content_hash(None) is None


# ───────────── extractors ─────────────

def test_the_generic_extractor_finds_the_posting_and_drops_the_site():
    found = x.extract(REAL, "https://careers.northwind.test/jobs/1")
    assert found.extractor == "generic" and found.method == "dom_score" and found.confidence >= 0.6
    assert "6+ years of experience" in found.text and "Istio" in found.text
    for noise in ("Sign in", "cookies", "Similar jobs", "All rights reserved", "Data Engineer"):
        assert noise not in found.text, noise


def test_json_ld_is_preferred_and_unescaped():
    found = x.extract(page("json_ld"), "https://globex.test/jobs/7")
    assert found.method == "json_ld" and found.confidence >= 0.9
    assert "4+ years of experience" in found.text and "&lt;" not in found.text and "<li>" not in found.text


def test_dom_candidates_are_scored_and_the_posting_ranks_first():
    blocks = x.scored_blocks(x._clean(x._soup(page("ambiguous"))))
    assert [b.id for b in blocks] == ["b1", "b2"]
    assert "Machine Learning Engineer" in blocks[0].text and "engineering culture" in blocks[1].text
    assert blocks[0].score > blocks[1].score


def test_a_close_call_is_reported_as_low_confidence():
    """The ambiguous fixture has a culture block nearly as strong as the posting: the extractor says so."""
    from app.services.jd_recovery import AMBIGUOUS_BELOW

    found = x.extract(page("ambiguous"), "https://umbrella.test/jobs/3")
    assert found.method == "dom_score" and found.confidence < AMBIGUOUS_BELOW and len(found.candidates) == 2


def test_link_heavy_blocks_score_lower():
    from bs4 import BeautifulSoup

    links = BeautifulSoup("<div>" + "".join(f"<a href='/j/{i}'>Senior engineer job number {i} apply</a> "
                                             for i in range(30)) + "</div>", "html.parser").div
    prose = BeautifulSoup(f"<div>{'<p>We are looking for an engineer to build and run services.</p>' * 12}</div>",
                          "html.parser").div
    assert x.score_block(links, x.html_to_text(str(links))) < x.score_block(prose, x.html_to_text(str(prose)))


def test_site_extractors_are_chosen_by_host():
    assert x.extractor_for("https://boards.greenhouse.io/acme/jobs/123").name == "greenhouse"
    assert x.extractor_for("https://job-boards.greenhouse.io/acme/jobs/123").name == "greenhouse"
    assert x.extractor_for("https://jobs.lever.co/acme/0c6c7b9e-3a4f-4f0b-9d77-1c2b3d4e5f60").name == "lever"
    assert x.extractor_for("https://jobs.ashbyhq.com/acme/1").name == "ashby"
    assert x.extractor_for("https://apply.workable.com/acme/j/ABC123/").name == "workable"
    assert x.extractor_for("https://www.linkedin.com/jobs/view/123").name == "linkedin"
    assert x.extractor_for("https://in.indeed.com/viewjob?jk=1").name == "indeed"
    assert x.extractor_for("https://careers.acme.test/jobs/1").name == "generic"
    assert x.extractor_for("https://notgreenhouse.io.evil.test/x").name == "generic"


def test_greenhouse_and_lever_use_their_documented_json_endpoints():
    greenhouse = x.extractor_for("https://boards.greenhouse.io/acme/jobs/4012345")
    assert greenhouse.structured_url("https://boards.greenhouse.io/acme/jobs/4012345") == \
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs/4012345"
    body = {"content": "&lt;h2&gt;About the role&lt;/h2&gt;&lt;ul&gt;&lt;li&gt;Build APIs&lt;/li&gt;&lt;/ul&gt;"}
    assert [line for line in greenhouse.from_structured(body).splitlines() if line] == ["About the role",
                                                                                        "- Build APIs"]
    lever = x.extractor_for("https://jobs.lever.co/acme/0c6c7b9e-3a4f-4f0b-9d77-1c2b3d4e5f60")
    assert lever.structured_url("https://jobs.lever.co/acme/0c6c7b9e-3a4f-4f0b-9d77-1c2b3d4e5f60") == \
        "https://api.lever.co/v0/postings/acme/0c6c7b9e-3a4f-4f0b-9d77-1c2b3d4e5f60"
    text = lever.from_structured({"descriptionPlain": "We build tools.",
                                  "lists": [{"text": "Requirements", "content": "<li>Python</li>"}],
                                  "additionalPlain": "Remote friendly."})
    assert text == "We build tools.\n\nRequirements:\n- Python\n\nRemote friendly."
    assert x.extractor_for("https://careers.acme.test/j/1").structured_url("https://careers.acme.test/j/1") is None


def test_linkedin_and_indeed_containers_win_over_the_rest_of_the_page():
    body = ("<p>We are looking for a product designer to lead research and design for our mobile app.</p>"
            "<h3>Responsibilities</h3><ul><li>Own the design of new features end to end</li>"
            "<li>Run user research and usability tests</li></ul><h3>Requirements</h3><ul>"
            "<li>5+ years of experience in product design</li><li>Strong Figma skills</li></ul>"
            "<p>Benefits: hybrid work, health insurance, learning budget and a team that values craft.</p>")
    noise = "<div class='people-also-viewed'><a>Designer at Foo</a><a>Designer at Bar</a></div>"
    linkedin = x.extract(f"<html><body>{noise}<div class='show-more-less-html__markup'>{body}</div>{noise}</body></html>",
                         "https://www.linkedin.com/jobs/view/1")
    assert linkedin.extractor == "linkedin" and linkedin.method == "selector" and "Figma" in linkedin.text
    indeed = x.extract(f"<html><body>{noise}<div id='jobDescriptionText'>{body}</div></body></html>",
                       "https://www.indeed.com/viewjob?jk=1")
    assert indeed.extractor == "indeed" and indeed.method == "selector" and "Designer at Foo" not in indeed.text


def test_workable_keeps_description_requirements_and_benefits():
    html = ("<html><body><section data-ui='job-description'><h2>Description</h2><p>We are looking for a support "
            "engineer to help customers succeed with our platform and to own escalations.</p></section>"
            "<section data-ui='job-requirements'><h2>Requirements</h2><ul><li>3+ years of experience in technical "
            "support</li><li>Strong SQL skills</li><li>Excellent written English</li></ul></section>"
            "<section data-ui='job-benefits'><h2>Benefits</h2><p>Remote work, private health insurance and a "
            "learning budget for every member of the team, plus 25 days of holiday.</p></section></body></html>")
    found = x.extract(html, "https://apply.workable.com/acme/j/ABC/")
    assert found.extractor == "workable" and found.method == "selector"
    assert "support engineer" in found.text and "SQL" in found.text and "25 days" in found.text


def test_assemble_only_ever_returns_fetched_text_in_page_order():
    blocks = [x.Block("b1", "second", 1, order=5), x.Block("b2", "first", 2, order=1), x.Block("b3", "unused", 3)]
    assert x.assemble(blocks, ["b1", "b2", "nope"]) == "first\n\nsecond"
    assert x.assemble(blocks, []) == ""


def test_the_mock_model_pick_is_the_most_posting_like_block():
    blocks = [{"id": "b1", "text": "Our culture: we collaborate and design things together as a team."},
              {"id": "b2", "text": "Responsibilities: build models. Requirements: 3+ years of experience."}]
    assert x.likely_blocks(blocks) == ["b2"] and x.likely_blocks([]) == []
