"""Broken ligature glyphs in extracted PDF text (app.services.text_repair): restored deterministically before any
parser reads the text, and nothing else is touched."""
import pytest

from app.services import document_text
from app.services.text_repair import known, mappings, repair_glyphs

# Extracted from a real resume PDF whose fonts mapped "ti" to Y, and "tf" to [ in one font and B in another.
BROKEN = """AI Engineer with 11+ years of experience specializing in architecYng scalable cloud-naYve pla[orms, real-Yme
data streaming, and producYon-grade LLM infrastructure. Proven experYse in designing AI agent workflows, opYmizing
Retrieval-Augmented GeneraYon (RAG) pipelines. Highly skilled in LLM orchestraYon, mulY-tenant SaaS and product
capabiliYes.
PlaBorm & Infrastructure: AWS, Kubernetes, eBay, JavaScript
PlaBorm & Infrastructure: Docker"""


def test_the_broken_words_are_restored():
    repaired = repair_glyphs(BROKEN)
    for word in ("architecting", "cloud-native", "platforms", "real-time", "production-grade", "expertise",
                 "optimizing", "Generation", "orchestration", "multi-tenant", "capabilities", "Platform &"):
        assert word in repaired, word
    assert mappings(BROKEN) == {"Y": "ti", "[": "tf", "B": "tf"}


def test_real_mixed_case_words_and_tokens_are_kept():
    repaired = repair_glyphs(BROKEN)
    assert "eBay" in repaired and "JavaScript" in repaired and "Kubernetes" in repaired


@pytest.mark.parametrize("text", [
    "Built JavaScript apps for eBay on macOS and iOS; GraphQL, k8s, C#/.NET, PostgreSQL, McKinsey, DeepMind.",
    "Reduced p95 latency by 45% (from 1.2s to 650ms) across Kafka, i18n and l10n pipelines — AT&T, C++ and Node.js.",
    "", "Plain text with nothing odd in it.\n\n- a bullet\n"])
def test_clean_text_comes_back_byte_identical(text):
    assert repair_glyphs(text) == text


def test_one_odd_letter_is_not_a_mapping():
    text = "Shipped the iPhone app and the YouTube channel."
    assert mappings(text) == {} and repair_glyphs(text) == text


def test_known_reads_inflections_and_hyphenated_words():
    assert known("architecting") and known("capabilities") and known("multi-tenant") and known("expertise")
    assert not known("architecyng") and not known("plaborm")


def test_a_pdf_upload_is_repaired_before_it_is_parsed(monkeypatch):
    class Page:
        def extract_text(self):
            return BROKEN

    class Reader:
        def __init__(self, stream):
            self.pages = [Page()]

    import pypdf

    monkeypatch.setattr(pypdf, "PdfReader", Reader)
    text = document_text.extract(document_text.PDF, b"%PDF-1.4")
    assert "architecting" in text and "architecYng" not in text
    assert document_text.extract(document_text.TXT, "architecYng".encode()) == "architecYng", "only PDF text"
