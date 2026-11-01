"""The text of an uploaded document (PDF, DOCX or TXT): resumes and job descriptions read the same way.

PDF text has its broken ligature glyphs restored (app.services.text_repair) before anything parses it, so the regex
parser, the AI parser and grounding all read "architecting", not "architecYng". Raises whatever the reader raises for
an unreadable file; callers turn that into their own message.

With V2 on, a PDF is read by app.services.resume_extraction instead (PDFium, which honours the /ActualText some
producers write for ligatures, plus line-break hyphen joining); off, the path below is unchanged. A resume upload
passes the account's choice (`v2`, Settings → Resumes); everything else follows the server (RESUME_EXTRACTION_V2).
"""
from __future__ import annotations

from io import BytesIO

from app.services.text_repair import repair_glyphs
from app.settings import get_settings

PDF, DOCX, TXT = ("application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                  "text/plain")


def extract(kind: str, data: bytes, *, errors: str = "strict", v2: bool | None = None) -> str:
    """Raw text (not cleaned: callers run clean_raw_text) of a file of media type `kind`. `v2` None follows the
    server's RESUME_EXTRACTION_V2."""
    if kind == PDF:
        if get_settings().resume_extraction_v2 if v2 is None else v2:
            from app.services.resume_extraction import pipeline

            return pipeline.extract(kind, data, errors=errors).text
        from pypdf import PdfReader

        return repair_glyphs("\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(data)).pages))
    return read_plain(kind, data, errors=errors)


def read_plain(kind: str, data: bytes, *, errors: str = "strict") -> str:
    """A TXT or DOCX file's text, as written."""
    if kind == TXT:
        return data.decode("utf-8", errors=errors)
    from docx import Document

    return "\n".join(p.text for p in Document(BytesIO(data)).paragraphs)
