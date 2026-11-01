"""Resume extraction V2, Phase 2 (app.services.resume_extraction): the PDFium text layer, normalisation and the quality
report, and the RESUME_EXTRACTION_V2 switch in document_text. Off, an upload reads exactly as before."""
from __future__ import annotations

from io import BytesIO

import pytest

from app.services import document_text
from app.services.resume_extraction import normalize, pipeline, quality, text_layer
from app.settings import get_settings

DOCS = __import__("pathlib").Path(__file__).parent / "fixtures" / "resume_extraction" / "documents"


def _pdf(name: str) -> bytes:
    return (DOCS / name).read_bytes()


@pytest.fixture
def v2(monkeypatch):
    monkeypatch.setattr(get_settings(), "resume_extraction_v2", True)


# ───────────── the switch ─────────────

def test_off_reads_a_pdf_exactly_as_before(monkeypatch):
    from pypdf import PdfReader

    from app.services.text_repair import repair_glyphs

    monkeypatch.setattr(get_settings(), "resume_extraction_v2", False)
    monkeypatch.setattr(pipeline, "extract", lambda *a, **k: pytest.fail("V2 ran while off"))
    data = _pdf("ana-single-actualtext.pdf")
    before = repair_glyphs("\n".join(p.extract_text() or "" for p in PdfReader(BytesIO(data)).pages))
    assert document_text.extract(document_text.PDF, data) == before
    assert "Kaga" in before, "pypdf ignores ActualText: the bug V2 fixes"


def test_on_reads_actualtext_and_leaves_docx_and_txt_as_they_were(v2):
    text = document_text.extract(document_text.PDF, _pdf("ana-single-actualtext.pdf"))
    assert "Apache Kafka" in text and "Kaga" not in text and "Software Engineer" in text
    assert "https://linkedin.example.test/in/anawhitfield" in text
    for name, kind in (("ana.docx", document_text.DOCX), ("ben.txt", document_text.TXT)):
        assert document_text.extract(kind, (DOCS / name).read_bytes()) == document_text.read_plain(
            kind, (DOCS / name).read_bytes())


# ───────────── [A] text layer ─────────────

def test_words_keep_their_page_box_and_font():
    layer = text_layer.read(_pdf("ana-single-clean.pdf"))
    assert layer.source == "pdfium" and len(layer.pages) >= 1
    first = layer.pages[0].lines[0]
    assert first.text == "Ana Whitfield"
    name = first.words[0]
    assert name.page == 1 and name.box is not None and name.box.x1 > name.box.x0 and name.box.y1 > name.box.y0
    assert name.font == "LigatureFixtureSans-Bold" and name.bold
    body = next(line for line in layer.pages[0].lines if line.text.startswith("Data platform engineer"))
    assert not body.words[0].bold and body.words[0].size < name.size


def test_a_comma_below_the_baseline_stays_on_its_line():
    lines = [line.text for line in text_layer.read(_pdf("ana-single-clean.pdf")).pages[0].lines]
    assert "Senior Data Platform Engineer | Northwind Logistics | Seattle, WA | Jan 2021 \u2013 Present" in lines


def test_a_file_pdfium_cannot_open_falls_back_to_pypdf(monkeypatch):
    def fail(data):
        raise RuntimeError("cannot open")

    monkeypatch.setattr(text_layer, "read_pdfium", fail)
    layer = text_layer.read(_pdf("ana-single-clean.pdf"))
    assert layer.source == "pypdf" and "Ana Whitfield" in layer.text


def test_bad_characters_are_counted():
    assert text_layer.flags_of("a\ufffdb \ue081 (cid:12) x\x07 ﬁ") == {
        "replacement_char": 1, "private_use": 1, "cid_code": 1, "control_char": 1, "ligature_char": 1}
    assert text_layer.flags_of("plain text") == {}


def test_a_soft_hyphen_is_written_back_at_its_line_end():
    from app.services.resume_extraction.models import Line, Page, TextLayer, Word

    layer = TextLayer("pdfium", [Page(1, 600, 800, [Line(1, [Word("produc", 1)], soft_hyphen=True),
                                                   Line(1, [Word("tion", 1)])])])
    assert layer.text == "produc-\ntion"
    assert normalize.normalize(layer.text) == "production"


# ───────────── [A] normalisation ─────────────

def test_only_artefact_characters_change():
    assert normalize.normalize_chars("Certiﬁcation of workﬂow, ａｂｃ１２\xa0x\u200by soft\xadware") == \
        "Certification of workflow, abc12 xy software"
    meaningful = "Area 120 m², ½ day, Acme™, ① first, café, naïve, 25°C, x² + y²"
    assert normalize.normalize_chars(meaningful) == meaningful, "never NFKC: these carry meaning"


@pytest.mark.parametrize(("split", "joined"), [
    ("optimiza-\ntion", "optimization"),  # a broken word: joined
    ("real-\ntime", "real-time"),  # two words: the hyphen is real
    ("click-\nthrough", "click-through"),
    ("Kuber-\nnetes", "Kuber-netes"),  # unknown either way: the hyphen stays, never a guess
    ("end-\nTo", "end-\nTo"),  # a capital after the break: left alone
    ("2019-\n2021", "2019-\n2021"),  # not letters
])
def test_a_word_split_by_a_line_break_is_joined_only_on_evidence(split, joined):
    assert normalize.join_lines(split) == joined


# ───────────── [D] quality ─────────────

@pytest.mark.parametrize(("name", "route"), [
    ("ana-single-clean.pdf", "clean"), ("ana-single-actualtext.pdf", "clean"), ("ana-single-broken.pdf", "poor"),
    ("ben-two_column-broken.pdf", "poor"), ("chloe-sidebar-actualtext.pdf", "clean"),
    ("dev-table-broken.pdf", "poor")])
def test_the_quality_report_routes_each_fixture(name, route):
    report = pipeline.extract(document_text.PDF, _pdf(name)).quality
    assert report.route == route, report.reasons
    assert set(report.summary()) >= {"route", "reasons", "oov_rate", "suspect_words", "flags"}
    assert not any(word in str(report.summary()) for word in ("Kafka", "Whitfield")), "counts only, never words"


def test_a_page_without_text_is_a_scan():
    from pypdf import PdfWriter

    writer, out = PdfWriter(), BytesIO()
    writer.add_blank_page(612, 792)
    writer.write(out)
    report = pipeline.extract(document_text.PDF, out.getvalue()).quality
    assert report.route == "no_text"


@pytest.mark.parametrize(("word", "is_broken"), [
    ("architecYng", True), ("pla[orms", True), ("real-Yme", True), ("aCenYon", True), ("PlaBorm", True),
    ("cloud-naYve", True), ("BigQuery", False), ("PyTorch", False), ("GitHub", False), ("eBay", False),
    ("PoCs", False), ("DevOps", False), ("Event-Driven", False), ("JavaScript", False),
    ("1ziAhF8QM60hJ", False)])
def test_broken_glyph_words_are_told_from_product_names(word, is_broken):
    assert quality.broken(word) is is_broken
