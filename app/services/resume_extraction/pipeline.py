"""Resume extraction V2: one uploaded file → its text, the text layer it came from, and a quality report.

    PDF   PDFium text layer (ActualText honoured; pypdf if PDFium cannot open it)  [A]
          → normalize (artefact characters, words split by a line break)        [A]
          → text_repair.repair_glyphs (unchanged)                              [A]
          → quality report on the text layer                                   [D]
    DOCX / TXT   read exactly as before (document_text); a quality report is added

Runs only when `RESUME_EXTRACTION_V2` is on (document_text.extract decides); off, nothing here is called. Fast and
deterministic, so it may run inside the upload request. Logs counts and the route, never the text.
"""
from __future__ import annotations

import logging

from app.services.resume_extraction import normalize, quality, text_layer
from app.services.resume_extraction.models import Extraction
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
        log.info("resume text read: source=%s pages=%d words=%d route=%s flags=%s", layer.source,
                 len(layer.pages), report.words, report.route, report.flags)
        return Extraction(text=text, text_layer=raw, quality=report, layer=layer)
    from app.services import document_text

    raw = document_text.read_plain(kind, data, errors=errors)
    return Extraction(text=raw, text_layer=raw, quality=quality.assess(raw, text_layer.flags_of(raw)))
