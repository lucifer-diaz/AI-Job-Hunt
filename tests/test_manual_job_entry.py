"""Discovery → Add one manually: a job from pasted text, or from an uploaded description (PDF, DOCX, TXT).

An upload is only read into a draft — nothing is saved until the person checks it and adds it as text. Text a person
gave is theirs (description_source USER), so description recovery never replaces it.
"""
import uuid

from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.main import app
from app.models import Job
from app.services.exports import markdown_to_simple_pdf

client = TestClient(app)
DESCRIPTION = "We are hiring a lead engineer to build Python services and mentor a small team. " * 5


def posting(**over):
    tag = uuid.uuid4().hex[:8]
    return {"title": "Lead Engineer", "company": f"Acme {tag}", "location": "Remote", "description": DESCRIPTION,
            **over}


def test_a_pasted_description_adds_a_job_owned_by_the_person():
    out = client.post("/api/jobs/from-text", json=posting())
    assert out.status_code == 200, out.text
    body = out.json()
    assert body["created"] is True
    with SessionLocal() as db:
        job = db.get(Job, body["job"]["id"])
        assert job.source == "manual" and job.url == ""
        assert job.description.startswith("We are hiring")
        assert job.description_source == "USER"


def test_adding_the_same_text_twice_is_one_job():
    data = posting()
    first = client.post("/api/jobs/from-text", json=data).json()
    again = client.post("/api/jobs/from-text", json={**data, "title": "lead engineer "}).json()
    assert again["created"] is False and again["job"]["id"] == first["job"]["id"]


def test_text_with_a_link_is_the_same_job_as_adding_the_link():
    url = f"https://example.com/jobs/{uuid.uuid4().hex}"
    by_link = client.post("/api/jobs/from-url", json={"url": url, **posting()}).json()
    by_text = client.post("/api/jobs/from-text", json=posting(url=url)).json()
    assert by_text["created"] is False and by_text["job"]["id"] == by_link["job"]["id"]


def test_pasted_text_needs_a_title_company_and_a_real_description():
    assert client.post("/api/jobs/from-text", json=posting(description="Too short")).status_code == 422
    assert client.post("/api/jobs/from-text", json=posting(company="  ")).status_code == 422
    assert client.post("/api/jobs/from-text", json=posting(url="javascript:alert(1)")).status_code == 422


def test_a_pdf_is_read_into_a_draft_without_saving_anything():
    pdf = markdown_to_simple_pdf(f"Job title: Staff Data Engineer\n\nCompany: Globex\n\nLocation: Berlin\n\n{DESCRIPTION}")
    with SessionLocal() as db:
        before = db.query(Job).count()
    out = client.post("/api/jobs/read-file", files={"file": ("posting.pdf", pdf, "application/pdf")})
    assert out.status_code == 200, out.text
    draft = out.json()
    assert draft["title"] == "Staff Data Engineer" and draft["company"] == "Globex" and draft["location"] == "Berlin"
    assert "lead engineer" in draft["description"]
    with SessionLocal() as db:
        assert db.query(Job).count() == before


def test_unlabelled_text_leaves_title_and_company_for_the_person():
    out = client.post("/api/jobs/read-file", files={"file": ("jd.txt", DESCRIPTION.encode(), "text/plain")}).json()
    assert out["title"] == "" and out["company"] == "" and out["description"].startswith("We are hiring")


def test_an_unsupported_or_empty_file_is_refused():
    assert client.post("/api/jobs/read-file", files={"file": ("jd.png", b"\x89PNG", "image/png")}).status_code == 415
    assert client.post("/api/jobs/read-file", files={"file": ("jd.txt", b"", "text/plain")}).status_code == 400
    assert client.post("/api/jobs/read-file", files={"file": ("jd.pdf", b"not a pdf", "application/pdf")}).status_code == 422
