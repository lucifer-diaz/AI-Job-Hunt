"""Search scope: a job its own search did not ask for (role, primary skill, place) is hidden. Deterministic, no model;
only a clear miss counts."""
from types import SimpleNamespace

import pytest

from app.services.search_scope import LOCATION, ROLE, SKILL, assess

JD = "We build Python services on AWS. You will design APIs and own them in production."


def job(title, location="Bengaluru, Karnataka, India", description=JD):
    return SimpleNamespace(title=title, location=location, description=description)


def verdict(title, *, roles=("Backend Engineer",), location="Bengaluru, India", skills=(), distance=50, **kw):
    return assess(job(title, **kw), list(roles), location, list(skills), distance=distance)


@pytest.mark.parametrize("title", ["Senior Backend Engineer", "Backend Developer (Python)", "Backend Platform Lead"])
def test_titles_naming_a_role_are_kept(title):
    assert verdict(title).match


@pytest.mark.parametrize("title", ["Data Analyst", "Frontend Engineer", "HR Business Partner"])
def test_titles_naming_none_of_the_roles_are_hidden(title):
    out = verdict(title)
    assert not out.match and out.code == ROLE and "Backend Engineer" in out.reason


def test_a_primary_skill_in_the_title_counts_as_the_role():
    assert verdict("Python Developer", skills=["Python"]).match
    assert not verdict("Java Developer", skills=["Python"]).match


def test_roles_with_only_generic_words_do_not_filter_titles():
    assert verdict("Anything At All", roles=["Senior Engineer"]).match


def test_the_skill_must_appear_in_the_title_or_description():
    out = verdict("Backend Engineer", skills=["Java"])
    assert not out.match and out.code == SKILL
    assert verdict("Backend Engineer", skills=["Go", "Python"]).match
    assert verdict("Backend Engineer", skills=["Java"], description="").match, "no description yet: kept"


def test_another_country_is_hidden_and_the_same_one_kept():
    out = assess(job("Backend Engineer", location="Austin, TX, United States"), ["Backend Engineer"],
                 "Bengaluru, India", [], distance=50)
    assert not out.match and out.code == LOCATION
    assert assess(job("Backend Engineer", location="Whitefield, Bangalore"), ["Backend Engineer"],
                  "Bengaluru, India", [], distance=50).match, "a nearby place in the radius"
    assert assess(job("Backend Engineer", location="Pune, India"), ["Backend Engineer"],
                  "Bengaluru, India", [], distance=50).match, "same country, radius left to the board"


def test_a_zero_radius_wants_the_same_city():
    hidden = assess(job("Backend Engineer", location="Pune, India"), ["Backend Engineer"], "Bengaluru, India", [],
                    distance=0)
    assert not hidden.match and hidden.code == LOCATION
    assert assess(job("Backend Engineer", location="Bangalore, Karnataka"), ["Backend Engineer"], "Bengaluru, India",
                  [], distance=0).match, "aliases are the same city"


@pytest.mark.parametrize("where,searched", [("", "Bengaluru, India"), ("Remote", "Bengaluru, India"),
                                            ("San Jose, CA", ""), ("Austin, TX, USA", "Remote"),
                                            ("Somewhere", "Bengaluru, India")])
def test_unclear_locations_are_kept(where, searched):
    assert assess(job("Backend Engineer", location=where), ["Backend Engineer"], searched, [], distance=50).match
