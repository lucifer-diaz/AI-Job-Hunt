import uuid

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader
from io import BytesIO

from app.main import app
from app.services.exports import markdown_to_simple_pdf
from app.services.resume_format import parse_resume, render_legacy, render_resume, to_plain
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
    assert "Tailored summary." in markdown and "- Most relevant: LangGraph" in markdown
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


def test_tailoring_keeps_structure_and_revert_restores_master_bullet():
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
    assert "- Built and operated production backend and AI systems." in markdown
    bullet = next(e for e in resume["edits"] if e["section"].startswith("EXPERIENCE"))
    assert bullet["section"] == "EXPERIENCE · AI Consultant · Flow AI"
    reverted = client.patch(f"/api/resumes/{resume['id']}/edits/{bullet['index']}", json={"status": "reverted"}).json()
    restored = reverted["resume"]["markdown"]
    assert "Built and operated production backend" not in restored
    assert "- Built production AI agents adopted by 100+ brands" in restored  # master bullets come back
    docx = client.get(f"/api/jobs/{job['id']}/resume.docx")
    pdf = client.get(f"/api/jobs/{job['id']}/resume.pdf")
    assert docx.status_code == 200 and pdf.status_code == 200
