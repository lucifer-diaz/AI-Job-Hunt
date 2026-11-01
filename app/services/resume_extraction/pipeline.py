"""Resume extraction V2: one uploaded file → its text, the text layer it came from, and a quality report.

    PDF   PDFium text layer (ActualText honoured; pypdf if PDFium cannot open it)  [A]
          → normalize (artefact characters, words split by a line break)        [A]
          → text_repair.repair_glyphs (unchanged)                              [A]
          → quality report on the text layer                                   [D]
    DOCX  paragraphs and table rows in order, with styles and list items (docx_reader)
    TXT   as before (document_text.read_plain)
    then  sections (sections.detect) and the parse view (structure.view) for the parser                [F]

Runs only when `RESUME_EXTRACTION_V2` is on (document_text.extract decides); off, nothing here is called. Fast and
deterministic, so it may run inside the upload request. Logs counts and the route, never the text.
"""
from __future__ import annotations

import logging

from app.services.resume_extraction import docx_reader, normalize, quality, sections, structure, text_layer
from app.services.resume_extraction.models import Extraction, LineInfo, TextLayer
from app.services.text_repair import repair_glyphs

log = logging.getLogger("jobhunt.resume_extraction")

PDF = "application/pdf"


def extract(kind: str, data: bytes, *, errors: str = "strict") -> Extraction:
    """`Extraction.text` is raw text in document_text's sense: callers still run clean_raw_text."""
    if kind == PDF:
        layer = text_layer.read(data)
        raw = layer.text
        text = repair_glyphs(normalize.normalize(raw))
        report = quality.assess(raw, layer.flags, layer)
        out = _structured(Extraction(text=text, text_layer=raw, quality=report, layer=layer))
        log.info("resume text read: source=%s pages=%d words=%d route=%s flags=%s sections=%s", layer.source,
                 len(layer.pages), report.words, report.route, report.flags,
                 [f"{s['kind']}:{s['method']}" for s in out.sections])
        return out
    from app.services import document_text

    if kind == document_text.DOCX:
        lines = docx_reader.read(data)
        raw = docx_reader.text(lines)
        report = quality.assess(raw, text_layer.flags_of(raw))
        return _structured(Extraction(text=raw, text_layer=raw, quality=report), sources=lines)
    raw = document_text.read_plain(kind, data, errors=errors)
    return _structured(Extraction(text=raw, text_layer=raw, quality=quality.assess(raw, text_layer.flags_of(raw))))


def _structured(out: Extraction, sources: list[LineInfo] | None = None) -> Extraction:
    """Sections and the parse view, from the text as it will be stored (after clean_raw_text) so that their line
    numbers are the stored text's."""
    from app.services.structured_resume import clean_raw_text

    infos = structure.line_infos(clean_raw_text(out.text), out.layer, sources)
    headings = sections.detect(infos)
    out.sections = [h.as_dict() for h in headings]
    out.view = structure.view(infos, headings)
    return out


def parse_view(text: str, layer: TextLayer | None = None) -> str:
    """The parse view of already cleaned text (and its layer, when the file had one)."""
    infos = structure.line_infos(text, layer)
    return structure.view(infos, sections.detect(infos))
