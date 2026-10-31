"""The text of an uploaded document (PDF, DOCX or TXT): resumes and job descriptions read the same way.

PDF text has its broken ligature glyphs restored (app.services.text_repair) before anything parses it, so the regex
parser, the AI parser and grounding all read "architecting", not "architecYng". Raises whatever the reader raises for
an unreadable file; callers turn that into their own message.
"""
from __future__ import annotations

from io import BytesIO

from app.services.text_repair import repair_glyphs

PDF, DOCX, TXT = ("application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                  "text/plain")


def extract(kind: str, data: bytes, *, errors: str = "strict") -> str:
    """Raw text (not cleaned: callers run clean_raw_text) of a file of media type `kind`."""
    if kind == PDF:
        from pypdf import PdfReader

        return repair_glyphs("\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(data)).pages))
    if kind == TXT:
        return data.decode("utf-8", errors=errors)
    from docx import Document

    return "\n".join(p.text for p in Document(BytesIO(data)).paragraphs)
