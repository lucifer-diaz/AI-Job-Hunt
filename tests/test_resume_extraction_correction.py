"""Resume extraction V2, Phase 6: corrections of words a PDF's text layer got wrong — from the page (OCR) or a
substitution the file uses consistently — never of a valid word, always recorded, and undoable. The OCR engine is
replaced by fixed readings except where a test needs the real one (skipped without Tesseract)."""
from __future__ import annotations

import io
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.auth import CurrentUser
from app.db import SessionLocal
from app.main import app
from app.services import worker_settings
from app.services.resume_extraction import align, correction, ocr, pipeline
from app.services.resume_extraction.models import Box, Line, Page, TextLayer, Word
from app.settings import get_settings
from tests.conftest import make_user

DOCS = Path(__file__).parent / "fixtures" / "resume_extraction" / "documents"
TESSERACT = shutil.which("tesseract") is not None
client = TestClient(app)


def _fix(fixes: list[correction.Correction]) -> dict[str, tuple[str, str]]:
    return {c.original: (c.corrected, c.method) for c in fixes}


# ───────────── the rules ─────────────

def test_the_page_corrects_an_unknown_word_it_reads_as_a_known_one():
    text = "Built streaming services on Kaga and Kaga Connect."
    fixes = correction.correct(text, [("Kaga", "Kafka"), ("Kaga", "Kafka")])
    assert _fix(fixes) == {"Kaga": ("Kafka", "ocr")}
    assert fixes[0].count == 2 and "the page reads “Kafka” there" in fixes[0].evidence
    assert correction.apply(text, fixes) == "Built streaming services on Kafka and Kafka Connect."


def test_a_valid_word_is_never_changed_whatever_the_page_or_a_rule_says():
    text = "Python, Hybrid cloud, Analytics, API Gateway, Synergy, Byzantine, Rhythm."
    readings = [("Python", "Pythoti"), ("Hybrid", "Hytirid"), ("Gateway", "Gatefkway")]
    assert correction.correct(text, readings) == []


def test_a_reading_far_from_the_word_is_not_taken():
    assert correction.correct("Owned Kaga.", [("Kaga", "Kubernetes")]) == []  # misaligned, not a reading of it


def test_a_rule_needs_two_different_unknown_words():
    one = "Shipped So4ware for three clients."
    assert correction.correct(one) == []  # one word: no rule, and no page to read
    two = "Shipped So4ware, and the team li4ed adoption."
    assert _fix(correction.correct(two)) == {"So4ware": ("Software", "rule"), "li4ed": ("lifted", "rule")}


def test_a_lower_case_glyph_is_fixed_only_where_it_makes_a_word():
    text = "Led the transformayon and optimizayon of reporting with Python and Hybrid analytics."
    fixes = _fix(correction.correct(text))
    assert fixes == {"transformayon": ("transformation", "rule"), "optimizayon": ("optimization", "rule")}


def test_the_page_and_a_rule_together_are_recorded_as_both():
    text = "Using hCps links while cuCing costs."
    fixes = correction.correct(text, [("hCps", "https"), ("cuCing", "cutting")])
    assert {c.method for c in fixes} == {"ocr+rule"}
    assert all(len(c.evidence) == 2 for c in fixes)


def test_an_undone_correction_is_not_applied():
    fix = correction.Correction("Kaga", "Kafka", "ocr", count=1).as_dict()
    assert correction.apply("Kaga", [fix]) == "Kafka"
    assert correction.apply("Kaga", [{**fix, "status": "undone"}]) == "Kaga"
    assert correction.apply("RAG/Kaga, Kagan", [fix]) == "RAG/Kafka, Kagan", "whole words only"


# ───────────── alignment and the engine ─────────────

def test_ocr_words_are_matched_to_the_text_layer_word_they_overlap():
    layer = TextLayer("pdfium", [Page(1, 600, 800, [Line(1, [Word("on", 1, Box(10, 700, 22, 710)),
                                                          Word("RAG/Kaga", 1, Box(25, 700, 80, 710))])])])
    words = [ocr.OcrWord("RAG/", 1, Box(25, 700, 48, 710), 95), ocr.OcrWord("Kafka", 1, Box(48, 700, 80, 710), 96),
             ocr.OcrWord("far", 1, Box(300, 100, 320, 110), 95)]
    assert align.readings(layer, words) == [("RAG/Kaga", "RAG/Kafka")]


@pytest.mark.skipif(not TESSERACT, reason="Tesseract is not installed")
def test_a_broken_pdf_is_read_right_with_the_page():
    out = pipeline.extract("application/pdf", (DOCS / "ben-two_column-broken.pdf").read_bytes())
    assert out.ocr == "tesseract" and out.quality.route == "poor"
    fixes = {c["original"]: c["corrected"] for c in out.corrections}
    assert fixes["Kaga"] == "Kafka" and fixes["li4ed"] == "lifted"
    assert "Kaga" in out.text and "Kafka" in out.clean_text and "Kaga" not in out.clean_text, "raw kept, clean fixed"


def test_a_clean_pdf_is_never_ocred(monkeypatch):
    monkeypatch.setattr(ocr, "read_pdf", lambda *a, **k: pytest.fail("OCR ran on a clean file"))
    out = pipeline.extract("application/pdf", (DOCS / "ana-single-actualtext.pdf").read_bytes())
    assert out.quality.route == "clean" and out.corrections == [] and out.ocr is None


def test_without_an_engine_rules_alone_correct(monkeypatch):
    monkeypatch.setattr(get_settings(), "resume_ocr_enabled", False)
    out = pipeline.extract("application/pdf", (DOCS / "ana-single-broken.pdf").read_bytes())
    assert out.ocr is None
    assert all(c["method"] == "rule" for c in out.corrections)
    assert "Kaga" in out.clean_text, "one distinct word: no rule, and no page to confirm it"


def test_ocr_that_runs_out_of_time_is_no_ocr():
    class Slow:
        name = "slow"

        def available(self):
            return True

        def read(self, png, timeout):
            raise TimeoutError

    assert ocr.read_pdf((DOCS / "ana-single-broken.pdf").read_bytes(), Slow(), timeout=1) is None


# ───────────── keep and undo ─────────────

@pytest.fixture
def user(monkeypatch):
    user_id = make_user(SessionLocal)
    monkeypatch.setattr(auth, "authenticate", lambda request: CurrentUser(user_id, f"{user_id}@jobhunt.test", False))
    monkeypatch.setattr(get_settings(), "llm_provider", "mock")
    db = SessionLocal()
    db.info["user_id"] = user_id
    worker_settings.update(db, user_id, {"resume_parsing": {"extraction_v2": True}})
    db.close()
    return user_id


def test_undoing_a_correction_reads_the_resume_with_the_files_word(user, monkeypatch):
    monkeypatch.setattr(ocr, "read_pdf", lambda *a, **k: [])  # no page readings: the rule corrections stay
    data = (DOCS / "chloe-sidebar-broken.pdf").read_bytes()
    out = client.post("/api/profile/resume", files={"file": ("cv.pdf", io.BytesIO(data), "application/pdf")})
    resume_id = out.json()["resume_id"]
    detail = client.get(f"/api/settings/resume?resume_id={resume_id}").json()["current"]
    fixes = {c["original"]: c for c in detail["extraction"]["corrections"]}
    assert "seCings" in fixes and fixes["seCings"]["corrected"] == "settings"
    bullet = "Wrote the settings service and its YAML schema used by 40 microservices."
    bullets = lambda d: [b["text"] for r in d["structured"]["experience"] for b in r["achievements"] + r["responsibilities"]]
    assert bullet in bullets(detail)
    undone = client.post(f"/api/resumes/{resume_id}/corrections", json={"id": fixes["seCings"]["id"], "action": "undo"})
    assert undone.status_code == 200, undone.text
    body = undone.json()
    assert next(c for c in body["extraction"]["corrections"] if c["original"] == "seCings")["status"] == "undone"
    assert bullet.replace("settings", "seCings") in bullets(body)
    again = client.post(f"/api/resumes/{resume_id}/corrections", json={"id": fixes["seCings"]["id"], "action": "keep"})
    assert bullet in bullets(again.json())
    assert client.post(f"/api/resumes/{resume_id}/corrections", json={"id": "nope", "action": "keep"}).status_code == 400
