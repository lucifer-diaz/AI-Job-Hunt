"""Resume extraction V2, Phase 3: where sections start (sections.detect), the parse view the regex parser reads
(structure.view), Word documents with their tables and list bullets (docx_reader), and the extraction record an upload
keeps (store). Companies and people here are invented."""
from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import auth
from app.auth import CurrentUser
from app.db import SessionLocal
from app.main import app
from app.models import Resume, ResumeExtraction
from app.services import document_text, worker_settings
from app.services import resume_verification as rv
from app.services.resume_extraction import docx_reader, pipeline, sections, structure
from app.services.resume_extraction.models import LineInfo
from app.services.structured_resume import parse_deterministic
from app.settings import get_settings
from tests.conftest import make_user

DOCS = Path(__file__).parent / "fixtures" / "resume_extraction" / "documents"
DOCX = document_text.DOCX
client = TestClient(app)


def lines(*rows: str | tuple[str, float]) -> list[LineInfo]:
    """Lines with a glyph height: a tuple is (text, size); a plain string is body text (size 7)."""
    return [LineInfo(text=r[0], size=r[1]) if isinstance(r, tuple) else LineInfo(text=r, size=7.0) for r in rows]


# ───────────── sections ─────────────

@pytest.mark.parametrize(("heading", "kind", "method"), [
    ("Core Skills", "skills", "synonym"), ("Areas of Expertise", "skills", "synonym"),
    ("Tech Stack", "skills", "synonym"), ("Work History", "experience", "synonym"),
    ("Career History", "experience", "synonym"), ("PROFESSIONAL EXPERIENCE", "experience", "synonym"),
    ("Licenses & Certifications", "certifications", "synonym"), ("Academic Background", "education", "synonym"),
    ("Technical Skills & Tools", "skills", "keyword"), ("Relevant Industry Experience", "experience", "keyword"),
    ("Selected Engagements", "other", "unknown")])
def test_headings_are_placed_by_synonym_keyword_or_not_at_all(heading, kind, method):
    assert sections.classify(heading) == (kind, method)


def test_an_unknown_heading_in_the_house_style_starts_its_own_section():
    doc = lines(("Alex Rivera", 14), ("STAFF ENGINEER", 9), "alex@example.test", ("SKILLS", 9), "Go, Kafka",
                ("SELECTED ENGAGEMENTS", 9), "Advised two fintech startups on platform design.",
                ("Platform Lead", 7.4), ("EXPERIENCE", 9), "Acme | Lead | Remote | 2020 - 2024")
    found = [(h.text, h.kind, h.method) for h in sections.detect(doc)]
    assert found == [("SKILLS", "skills", "synonym"), ("SELECTED ENGAGEMENTS", "other", "unknown"),
                     ("EXPERIENCE", "experience", "synonym")]
    # "STAFF ENGINEER" is the headline (above the first heading) and "Platform Lead" is not in the house style.


def test_the_header_keeps_a_headline_that_names_a_section_word():
    doc = lines(("Dana Ross", 14), ("Full Stack Developer", 9), ("Summary", 9), "Builds web apps.", ("Tech Stack", 9),
                "React, Node.js")
    assert [h.text for h in sections.detect(doc)] == ["Summary", "Tech Stack"]


def test_without_layout_only_headings_written_like_one_count():
    doc = [LineInfo(text=t) for t in ("Alex Rivera", "Core Skills", "Python, Go, tools", "Selected Engagements",
                                      "Technical Skills & Tools", "Career History", "Acme | Lead | 2020 - 2024")]
    assert [h.text for h in sections.detect(doc)] == ["Core Skills", "Technical Skills & Tools", "Career History"]


# ───────────── the parse view ─────────────

def test_roles_are_assembled_from_an_employer_line_and_a_title_line():
    doc = [LineInfo(text=t) for t in (
        "Pat Lee", "EXPERIENCE",
        "Northwind Remote, US | Oct 2025 - Sep 2026", "Platform Consultant",
        "• Designed the routing layer for three clients.",
        "Fabrikam Jan 2022 - Oct 2025", "Lead Engineer Remote, US | Jun 2024 - Oct 2025",
        "• Led the event platform.",
        "Engineer II Pune, IN | Jan 2022 - Jun 2024",
        "• Built the reporting service.",
        "Contoso Remote, US | Nov 2015 - Jan 2020", "Senior Engineer", "Nov 2017 - Jan 2020",
        "• Owned billing.", "Engineer", "Nov 2015 - Nov 2017", "• Wrote the importer.")]
    # Column gaps the page shows between the employer and the rest of its line:
    for info in doc:
        if info.text.startswith(("Northwind", "Fabrikam", "Lead Engineer", "Engineer II", "Contoso")):
            head, _, rest = info.text.partition(" ") if not info.text.startswith(("Lead Engineer", "Engineer II")) \
                else (" ".join(info.text.split()[:2]), " ", " ".join(info.text.split()[2:]))
            info.segments = [head, rest]
    data = parse_deterministic(structure.view(doc))
    assert [(r["job_title"], r["company"], r["location"], r["start_date"], r["end_date"]) for r in data["experience"]] == [
        ("Platform Consultant", "Northwind", "Remote, US", "2025-10", "2026-09"),
        ("Lead Engineer", "Fabrikam", "Remote, US", "2024-06", "2025-10"),
        ("Engineer II", "Fabrikam", "Pune, IN", "2022-01", "2024-06"),
        ("Senior Engineer", "Contoso", "Remote, US", "2017-11", "2020-01"),
        ("Engineer", "Contoso", "Remote, US", "2015-11", "2017-11")]
    assert [len(r["raw_bullets"]) for r in data["experience"]] == [1, 1, 1, 1, 1]


def test_bullets_without_a_character_split_where_sentences_end():
    doc = [LineInfo(text=t) for t in (
        "Experience", "Lead | Acme | Remote | 2020 - 2024", "Grew the team from 6 to 22 engineers across",
        "3 teams.", "Launched an internal developer portal that reduced service", "setup time to 1 day.")]
    role = parse_deterministic(structure.view(doc))["experience"][0]
    assert role["raw_bullets"] == ["Grew the team from 6 to 22 engineers across 3 teams.",
                                   "Launched an internal developer portal that reduced service setup time to 1 day."]


def test_wrapped_headers_skill_lists_and_projects_are_one_line_again():
    doc = [LineInfo(text=t) for t in (
        "Skills", "Engineering Leadership,", "Platform Strategy, Incident", "Management, Go",
        "Experience", "Staff Engineer | Wingtip | Boulder, CO | Sep 2014", "– Mar 2019", "- Built the order engine.",
        "Projects", "Pipeline Lint", "Open source linter for CI files with 1,200", "GitHub stars.")]
    data = parse_deterministic(structure.view(doc))
    assert [s["name"] for s in data["skills"]] == ["Engineering Leadership", "Platform Strategy",
                                                   "Incident Management", "Go"]
    role = data["experience"][0]
    assert (role["job_title"], role["start_date"], role["end_date"]) == ("Staff Engineer", "2014-09", "2019-03")
    assert data["projects"][0]["name"] == "Pipeline Lint"
    assert data["projects"][0]["description"] == "Open source linter for CI files with 1,200 GitHub stars."


def test_the_view_holds_only_the_documents_words():
    data = pipeline.extract("application/pdf", (DOCS / "chloe-sidebar-clean.pdf").read_bytes())
    marks = {"##", "###", "-", "|", "Summary", "Skills", "Experience", "Projects", "Education", "Certifications",
             "Additional", "information"}
    words = set(data.text.split()) | marks
    stray = {w.rstrip(":,") for w in data.view.split()} - {w.rstrip(":,") for w in words}
    assert not stray, stray


def test_columns_carry_over_from_the_text_layer():
    infos = structure.line_infos(*_cleaned("dev-table-clean.pdf"))
    row = next(i for i in infos if i.text.startswith("Frontend"))
    assert row.segments == ["Frontend", "React, TypeScript, Next.js, Tailwind CSS"]
    assert "Frontend: React, TypeScript, Next.js, Tailwind CSS" in structure.view(infos)


def _cleaned(name: str):
    from app.services.structured_resume import clean_raw_text

    out = pipeline.extract("application/pdf", (DOCS / name).read_bytes())
    return clean_raw_text(out.text), out.layer


# ───────────── Word documents ─────────────

def test_a_word_document_is_read_with_its_tables_styles_and_list_bullets():
    rows = docx_reader.read((DOCS / "dev.docx").read_bytes())
    table = next(r for r in rows if r.text.startswith("Frontend"))
    assert table.segments == ["Frontend", "React, TypeScript, Next.js, Tailwind CSS"]
    assert any(r.style and r.style.startswith("Heading") and r.text == "Tech Stack" for r in rows)
    assert any(r.bullet and r.text.startswith("Rebuilt the merchant onboarding flow") for r in rows)
    data = parse_deterministic(pipeline.extract(DOCX, (DOCS / "dev.docx").read_bytes()).view)
    assert {"React", "Brightlane SDK", "GitHub Actions"} <= {s["name"] for s in data["skills"]}
    assert len(data["experience"][0]["raw_bullets"]) == 3


def test_v2_reads_word_tables_and_off_does_not(monkeypatch):
    data = (DOCS / "dev.docx").read_bytes()
    monkeypatch.setattr(get_settings(), "resume_extraction_v2", False)
    assert "Brightlane SDK" not in document_text.extract(DOCX, data)
    monkeypatch.setattr(get_settings(), "resume_extraction_v2", True)
    assert "Brightlane SDK" in document_text.extract(DOCX, data)


# ───────────── the extraction record ─────────────

@pytest.fixture
def user(monkeypatch):
    user_id = make_user(SessionLocal)
    monkeypatch.setattr(auth, "authenticate", lambda request: CurrentUser(user_id, f"{user_id}@jobhunt.test", False))
    monkeypatch.setattr(get_settings(), "llm_provider", "mock")
    return user_id


def _upload(name: str, kind: str = "application/pdf") -> int:
    out = client.post("/api/profile/resume", files={"file": (name, io.BytesIO((DOCS / name).read_bytes()), kind)})
    assert out.status_code == 200, out.text
    return out.json()["resume_id"]


def _db(user_id):
    db = SessionLocal()
    db.info["user_id"] = user_id
    return db


def test_an_upload_read_with_v2_keeps_its_record_and_is_parsed_from_its_view(user):
    with _db(user) as db:
        worker_settings.update(db, user, {"resume_parsing": {"extraction_v2": True}})
    resume_id = _upload("chloe-sidebar-clean.pdf")
    with _db(user) as db:
        row = db.scalar(select(ResumeExtraction).where(ResumeExtraction.resume_id == resume_id))
        assert row is not None and row.source == "pdfium" and row.quality["route"] == "clean"
        assert [s["kind"] for s in row.sections] == ["skills", "education", "summary", "experience", "projects"]
        resume = db.get(Resume, resume_id)
        assert "## Skills" in row.parse_view and "## Skills" not in resume.raw_text, "the raw text is the file's"
        roles = [(r["job_title"], r["company"]) for r in resume.structured_data["experience"]]
        assert roles == [("Engineering Manager, Platform", "Adatum Cloud"), ("Staff Software Engineer", "Wingtip Toys")]
        assert "Tessellate DSL" in {s["name"] for s in resume.structured_data["skills"]}
    detail = client.get(f"/api/settings/resume?resume_id={resume_id}").json()["current"]
    assert detail["extraction"]["quality"]["route"] == "clean"
    assert detail["extraction"]["sections"][0] == {"text": "Areas of Expertise", "kind": "skills", "method": "synonym",
                                                   "signal": "text", "line": 3, "preview": "Engineering Leadership,"}
    assert detail["extraction"]["unplaced"] == 0


def test_without_v2_an_upload_has_no_record_and_reads_as_before(user, monkeypatch):
    monkeypatch.setattr(get_settings(), "resume_extraction_v2", False)
    resume_id = _upload("ana-single-clean.pdf")
    with _db(user) as db:
        assert db.scalar(select(ResumeExtraction).where(ResumeExtraction.resume_id == resume_id)) is None
        resume = db.get(Resume, resume_id)
        assert rv.parse_text(db, resume) == resume.raw_text
    assert client.get(f"/api/settings/resume?resume_id={resume_id}").json()["current"]["extraction"] is None


def test_deleting_a_version_removes_its_record(user):
    with _db(user) as db:
        worker_settings.update(db, user, {"resume_parsing": {"extraction_v2": True}})
    _upload("ana-single-clean.pdf")  # the first upload is active and cannot be deleted
    second = _upload("dev.docx", DOCX)
    assert client.delete(f"/api/resumes/{second}").status_code == 200
    with _db(user) as db:
        assert db.scalar(select(ResumeExtraction).where(ResumeExtraction.resume_id == second)) is None


def test_deleting_a_version_also_removes_its_parse_task(user):
    """SQLite does not cascade: a pending AI parse of a deleted version must not survive it (a later resume reusing
    the id could not queue its own)."""
    from app.models import ResumeParseTask

    with _db(user) as db:
        worker_settings.update(db, user, {"resume_parsing": {"extraction_v2": True}})
    _upload("ana-single-clean.pdf")
    second = _upload("dev.docx", DOCX)
    with _db(user) as db:
        assert db.scalar(select(ResumeParseTask).where(ResumeParseTask.resume_id == second)) is not None
    assert client.delete(f"/api/resumes/{second}").status_code == 200
    with _db(user) as db:
        assert db.scalar(select(ResumeParseTask).where(ResumeParseTask.resume_id == second)) is None


@pytest.mark.parametrize("heading", ["Expertise", "AREAS OF EXPERTISE", "Key Areas of Expertise", "Core Expertise",
                                     "Technical Expertise", "Domain Expertise", "Expertise & Tools",
                                     "Skills & Expertise", "Professional Expertise"])
def test_an_expertise_heading_is_a_skills_section(heading):
    long = "Platform engineer with nine years building streaming pipelines and internal tools for many teams."
    doc = [LineInfo(text=t, size=s) for t, s in (("Alex Rivera", 14), ("Summary", 9), (long, 7), (heading, 9),
                                                  ("Kafka, Kubernetes, Go", 7))]
    assert [h.kind for h in sections.detect(doc) if h.text == heading] == ["skills"]
    assert {s["name"] for s in parse_deterministic(structure.view(doc), marked=True)["skills"]} == {
        "Kafka", "Kubernetes", "Go"}


@pytest.mark.parametrize(("heading", "body", "kind"), [
    ("Professional Experience & Expertise", "Lead Engineer | Acme | Remote | Jan 2020 - Dec 2024", "experience"),
    ("Skills & Experience", "Lead Engineer | Acme | Remote | Jan 2020 - Dec 2024", "experience"),
    ("Experience & Expertise", "Kafka, Kubernetes, Go", "skills")])
def test_a_heading_naming_two_sections_is_placed_by_its_lines(heading, body, kind):
    long = "Platform engineer with nine years building streaming pipelines and internal tools for many teams."
    doc = [LineInfo(text=t, size=s) for t, s in (("Alex Rivera", 14), ("Summary", 9), (long, 7), (heading, 9),
                                                  (body, 7))]
    found = [(h.kind, h.method) for h in sections.detect(doc) if h.text == heading]
    assert found == [(kind, "keyword")], "still a guess the person sees"
