"""ATS keyword coverage (app.services.ats_keywords): keywords from the parsed posting, matched as whole words."""
from types import SimpleNamespace

from app.services import ats_keywords as ats

RESUME = """# Jane Doe
## Skills
- Languages: Python, Go, C++
- Platforms: Kafka, Kubernetes, AWS, Node.js, CI/CD
## Experience
### Lead Engineer | Zendrive | Bengaluru | Jun 2024 - Present
- Built distributed data systems on Kafka, cutting incidents by 45%
- Led a team of 8 engineers
"""
# What the Ranker stores today: requirement sentences, and no keyword list.
RANKING = {"keywords": [], "strong_matches": ["5+ years building distributed systems in Python",
                                              "Experience leading engineering teams"]}


def job(structured=None, skills=None):
    return SimpleNamespace(structured_description=structured, skills=skills or [])


def statuses(items):
    return {i["word"]: i["status"] for i in items}


def test_a_current_ranking_alone_no_longer_scores_zero_when_the_posting_was_parsed():
    posting = job({"required_skills": ["Python", "Kafka", "Kubernetes"], "preferred_skills": ["Terraform", "Node.js"],
                   "keywords": ["CI/CD", "distributed systems", "python"]})
    items = ats.coverage(posting, RESUME, RANKING)
    assert [i["word"] for i in items] == ["Python", "Kafka", "Kubernetes", "Terraform", "Node.js", "CI/CD",
                                          "distributed systems"], "required first, then preferred; no duplicates"
    assert statuses(items) == {"Python": "covered", "Kafka": "covered", "Kubernetes": "covered",
                               "Terraform": "missing", "Node.js": "covered", "CI/CD": "covered",
                               "distributed systems": "partial"}
    assert ats.score(items) == round(100 * (5 + 0.5) / 7)


def test_words_match_whole_not_inside_other_words():
    items = ats.coverage(job({"required_skills": ["Go", "Java", "C++", "C#", "nodejs"]}), RESUME)
    assert statuses(items) == {"Go": "covered", "Java": "missing", "C++": "covered", "C#": "missing",
                               "nodejs": "covered"}, "whole words only; 'nodejs' matches 'Node.js'"


def test_older_jobs_fall_back_to_posting_skills_then_the_ranking():
    assert [i["word"] for i in ats.coverage(job(None, ["Kafka", "Rust"]), RESUME)] == ["Kafka", "Rust"]
    legacy = ats.coverage(job(), RESUME, {"keywords": ["Kafka", "Scala"]})
    assert statuses(legacy) == {"Kafka": "covered", "Scala": "missing"}
    assert ats.score([]) is None, "no keywords: no score, never a misleading 0%"
