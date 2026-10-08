"""Test helpers for the one resume selection (app.services.resume_verification).

Saving resume text never changes which resume is active: it becomes a new, inactive version. A test that wants the
workflows to use some text says so, the way a person does in Settings: save it, then make it active."""
from __future__ import annotations


def use_resume_text(client, text: str, **profile) -> dict:
    """PUT the profile with this resume text and make that text's version the active resume. Returns the PUT body."""
    response = client.put("/api/profile", json={**profile, "master_resume": text})
    assert response.status_code == 200, response.text
    body = response.json()
    created = body.get("created_resume_id")
    if created is not None:
        activated = client.post(f"/api/resumes/{created}/activate")
        assert activated.status_code == 200, activated.text
    return body


def use_upload(client, upload_response) -> dict:
    """Make an uploaded file's version the active resume (an upload alone never does, after the first)."""
    body = upload_response.json() if hasattr(upload_response, "json") else upload_response
    activated = client.post(f"/api/resumes/{body['resume_id']}/activate")
    assert activated.status_code == 200, activated.text
    return body
