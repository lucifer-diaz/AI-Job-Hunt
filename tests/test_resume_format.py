import uuid

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader
from io import BytesIO

from app.main import app
from app.services.exports import markdown_to_simple_pdf
from app.services.resume_format import parse_resume, render_legacy, render_resume, strip_citations, to_plain
from app.settings import get_settings

client = TestClient(app)

MASTER = """# Rohan Roy
AI Engineering Leader
Mumbai, India | rohan@example.com | +91-9000000000

## Summary
AI Engineering Leader with 11+ years building distributed systems.

## Skills
- AI Engineering: LangGraph, OpenRouter, RAG pipelines
- Data: Kafka, Spark, Airflow

## Experience

### AI Consultant | Flow AI | Remote, USA | Oct 2025 - Present
- Built production AI agents adopted by 100+ brands, improving onboarding conversion by 28%.
- Designed a vendor-agnostic LLM orchestration layer with OpenRouter.

### Software Development Engineer 3 | Zendrive (India) | Bangalore, India | Jan 2022 - Jun 2024
- Reduced fleet reporting latency from 10 minutes to 500ms (20x) using Spark and Redis.

## Projects
- AI Marketing Platform: Multi-agent marketing platform using LangGraph and MCP.

## Education
### B.Tech, Computer Science | Kalyani Govt. Engg College | WB, India | Aug 2010 - Mar 2014
"""


@pytest.fixture(autouse=True)
def mock_llm(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_provider", "mock")


def test_to_plain_replaces_glyphs_pdf_fonts_cannot_draw():
    text = "Oct 2025 – Sep 2026 • cut 10× from ∼5 min, qualiﬁcation workﬂow “fast”"
    plain = to_plain(text)
    assert "Oct 2025 - Sep 2026 - cut 10x" in plain
    assert "qualification workflow \"fast\"" in plain
    plain.encode("cp1252")  # every remaining character is renderable


def test_parse_structured_resume_and_rejoin_wrapped_lines():
    parsed = parse_resume(MASTER.replace("improving onboarding conversion by 28%.",
                                         "improving onboarding\nconversion by 28%."))
    assert parsed.name == "Rohan Roy" and parsed.header_lines[0] == "AI Engineering Leader"
    assert [s.kind for s in parsed.sections] == ["summary", "skills", "experience", "projects", "education"]
    assert [r.company for r in parsed.roles] == ["Flow AI", "Zendrive (India)"]
    assert parsed.roles[0].dates == "Oct 2025 - Present"
    assert parsed.roles[0].bullets[0].endswith("improving onboarding conversion by 28%.")


def test_render_keeps_every_role_project_and_education():
    parsed = parse_resume(MASTER)
    markdown = render_resume(parsed, summary="Tailored summary.", top_skills=["LangGraph"],
                             role_bullets={0: ["Rewritten agent bullet with 28% conversion lift."]})
    assert "Tailored summary." in markdown
    assert "- AI Engineering: LangGraph, OpenRouter, RAG pipelines" in markdown  # every skills line is a bullet
    assert "Most relevant" not in markdown  # the groups are ordered instead of repeated in a lead line
    assert "### AI Consultant | Flow AI | Remote, USA | Oct 2025 - Present" in markdown
    assert "- Rewritten agent bullet with 28% conversion lift." in markdown
    assert "Designed a vendor-agnostic" not in markdown  # replaced bullets are not duplicated
    assert "- Reduced fleet reporting latency" in markdown  # untouched role keeps its bullets
    assert "## Projects" in markdown and "### B.Tech, Computer Science" in markdown
    assert "Source Resume" not in markdown


def test_legacy_fallback_never_appends_source():
    markdown = render_legacy("Name", "Headline", "Summary", ["Python"], ["Did a thing."])
    assert "## Selected Experience" in markdown and "Source Resume" not in markdown


def test_pdf_export_renders_headings_and_roles_cleanly():
    pdf = markdown_to_simple_pdf(render_resume(parse_resume(MASTER.replace(" - Present", " – Present"))))
    text = "\n".join(page.extract_text() for page in PdfReader(BytesIO(pdf)).pages)
    assert "SUMMARY" in text and "EXPERIENCE" in text
    assert "#Summary" not in text and "## " not in text
    assert "AI Consultant" in text and "Oct 2025 - Present" in text
    assert "■" not in text


def test_tailoring_keeps_structure_and_revert_restores_master_bullet(monkeypatch):
    from app.services.llm import LLMClient

    # The Tailor answers with a patch: one Flow AI bullet rewritten. The renderer keeps every other role and header.
    rewrite = "Built production AI agents for Flow AI, adopted by 100+ brands"
    monkeypatch.setattr(LLMClient, "_mock_tailoring", staticmethod(lambda user: {
        "summary": None, "skills_order": [], "highlights": [], "selected_evidence_ids": [],
        "role_updates": [{"role_index": 0, "bullets": [{"text": rewrite, "evidence_ids": []}]}]}))
    assert client.put("/api/profile", json={"name": "Rohan Roy", "headline": "AI Engineering Leader",
                                            "master_resume": MASTER}).status_code == 200
    tag = uuid.uuid4().hex[:8]
    job = client.post("/api/jobs/from-url", json={"url": f"https://example.com/format/{tag}", "title": "AI Lead",
                                                  "company": f"Format Co {tag}", "description": "Python AI agents"}).json()["job"]
    client.post(f"/api/jobs/{job['id']}/triage", json={"action": "shortlist"})
    assert client.post(f"/api/jobs/{job['id']}/tailor").status_code == 200
    resume = client.get(f"/api/tailor/{job['id']}").json()["resume"]
    markdown = resume["markdown"]
    assert "Source Resume" not in markdown
    for header in ("### AI Consultant | Flow AI", "### Software Development Engineer 3 | Zendrive (India)",
                   "## Projects", "### B.Tech, Computer Science"):
        assert header in markdown
    assert f"- {rewrite}" in markdown
    bullet = next(e for e in resume["edits"] if e["section"].startswith("EXPERIENCE"))
    assert bullet["section"] == "EXPERIENCE · AI Consultant · Flow AI"
    reverted = client.patch(f"/api/resumes/{resume['id']}/edits/{bullet['index']}", json={"status": "reverted"}).json()
    restored = reverted["resume"]["markdown"]
    assert rewrite not in restored
    assert "- Built production AI agents adopted by 100+ brands" in restored  # master bullets come back
    docx = client.get(f"/api/jobs/{job['id']}/resume.docx")
    pdf = client.get(f"/api/jobs/{job['id']}/resume.pdf")
    assert docx.status_code == 200 and pdf.status_code == 200


# A resume that writes the employer, location and dates under the role heading instead of beside it.
STACKED = """# Rohan Roy

**AI Engineering Leader**

Mumbai, India

## Core Skills

**AI and Generative AI:** LangGraph, RAG, MCP

**Data and Streaming:** Kafka, Spark

## Professional Experience

### AI Consultant

**Flow AI** | Remote, USA
October 2025 - September 2026

* Built production AI agents adopted by 100+ brands.
* Designed a vendor-agnostic LLM orchestration layer.

### Lead Engineer - Data Platform

**Zendrive Inc.** | Remote, USA
June 2024 - October 2025

* Reduced production incidents by 45%.
"""


def test_employer_and_dates_written_under_the_title_are_role_facts_not_bullets():
    parsed = parse_resume(STACKED)
    first, second = parsed.roles
    assert (first.title, first.company, first.location, first.dates) == (
        "AI Consultant", "Flow AI", "Remote, USA", "October 2025 - September 2026")
    assert (second.company, second.dates) == ("Zendrive Inc.", "June 2024 - October 2025")
    # The employer line is metadata: it never becomes an achievement bullet that tailoring can overwrite.
    assert first.bullets == ["Built production AI agents adopted by 100+ brands.",
                             "Designed a vendor-agnostic LLM orchestration layer."]


def test_tailored_roles_keep_their_employer_location_and_dates():
    markdown = render_resume(parse_resume(STACKED), top_skills=["LangGraph"],
                             role_bullets={0: ["Rewritten bullet about AI agents."]})
    assert "### AI Consultant | Flow AI | Remote, USA | October 2025 - September 2026" in markdown
    assert "### Lead Engineer - Data Platform | Zendrive Inc. | Remote, USA | June 2024 - October 2025" in markdown
    assert "- Rewritten bullet about AI agents." in markdown
    # Skill groups stay grouped, as bullets, and the tailored list stays short instead of repeating them all.
    assert "- **AI and Generative AI:** LangGraph, RAG, MCP" in markdown
    assert "- **Data and Streaming:** Kafka, Spark" in markdown

    text = "\n".join(page.extract_text() for page in PdfReader(BytesIO(markdown_to_simple_pdf(markdown))).pages)
    for fact in ("Flow AI", "Remote, USA", "October 2025 - September 2026", "Zendrive Inc.", "Kafka, Spark"):
        assert fact in text, fact
    assert "**" not in text  # emphasis is drawn, not printed


def test_skills_are_ordered_by_relevance_instead_of_being_repeated():
    markdown = render_resume(parse_resume(STACKED), top_skills=["Kafka", "Spark"])
    skills = [line for line in markdown.split("## Core Skills")[1].split("##")[0].splitlines() if line.startswith("- ")]
    # The group this job asks for leads, and nothing is duplicated into a "most relevant" line.
    assert skills == ["- **Data and Streaming:** Kafka, Spark", "- **AI and Generative AI:** LangGraph, RAG, MCP"]
    assert "Most relevant" not in markdown

    # Inside a group, the skills this job asks for come first; the rest keep their order and wording.
    reordered = render_resume(parse_resume(STACKED), top_skills=["MCP"])
    assert "- **AI and Generative AI:** MCP, LangGraph, RAG" in reordered

    untailored = render_resume(parse_resume(STACKED))
    assert untailored.split("## Core Skills")[1].strip().splitlines()[0] == "- **AI and Generative AI:** LangGraph, RAG, MCP"


def test_exports_show_contact_details_not_markdown_link_syntax():
    pdf = markdown_to_simple_pdf("# Rohan Roy\nEmail: [rohan@example.com](mailto:rohan@example.com)\n"
                                 "Portfolio: [my site](https://rohanroy.com)\n")
    text = "\n".join(page.extract_text() for page in PdfReader(BytesIO(pdf)).pages)
    assert "Email: rohan@example.com" in text and "](" not in text
    assert "my site (https://rohanroy.com)" in text


def test_pdf_draws_bold_runs_for_labels():
    pdf = markdown_to_simple_pdf("## Core Skills\n- **Data and Streaming:** Kafka, Spark\n")
    assert b"Helvetica-Bold" in pdf


def test_strip_citations_removes_evidence_ids_from_cover_letter_prose():
    letter = (
        "In my recent role as Lead Engineer at Zendrive Inc (evidence id 24), I reduced incidents by 45% "
        "(evidence id 23). I designed RAG pipelines (evidence ids 36, 37) and agents (Evidence IDs: E38 and E39). "
        "I mentored a team [E12]. I scaled throughput (E28, E29). Built a CRM bot, evidence id 40.\n\n"
        "Sincerely,\nRohan Roy"
    )
    assert strip_citations(letter) == (
        "In my recent role as Lead Engineer at Zendrive Inc, I reduced incidents by 45%. "
        "I designed RAG pipelines and agents. I mentored a team. I scaled throughput. Built a CRM bot.\n\n"
        "Sincerely,\nRohan Roy"
    )


def test_strip_citations_keeps_ordinary_parentheses_and_numbers():
    text = "I cut latency from 10 minutes to 500 ms (a 20x improvement) for 6 million events (2023)."
    assert strip_citations(text) == text
