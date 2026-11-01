"""[A] A Word document's text in reading order, with what Word says about each line. Deterministic, no model.

The old reader (document_text.read_plain) reads paragraphs only: a table — a skills grid, a dates column — is lost,
and a bullet made by Word's list numbering has no character, so the parser cannot see it. Here:

    paragraph   its text, its style ("Heading 1", "Title") and whether it is a list item (numbering or a List style)
    table row   one line, its cells as columns (LineInfo.segments; " | " in the text, a label cell as "Label: …")

Merged cells (Word repeats them per grid column) are read once. Nothing is added: only the document's own text.
"""
from __future__ import annotations

from io import BytesIO

from app.services.resume_extraction.models import LineInfo

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _is_list(paragraph) -> bool:
    ppr = paragraph._p.pPr
    if ppr is not None and ppr.numPr is not None:
        return True
    style = (paragraph.style.name if paragraph.style is not None else "") or ""
    return style.lower().startswith("list")


def _row(row) -> LineInfo | None:
    cells, seen = [], set()
    for cell in row.cells:
        if id(cell._tc) in seen:
            continue  # a merged cell, repeated for every grid column it spans
        seen.add(id(cell._tc))
        text = " ".join(cell.text.split())
        if text:
            cells.append(text)
    if not cells:
        return None
    return LineInfo(text=" | ".join(cells), segments=cells if len(cells) > 1 else None)


def read(data: bytes) -> list[LineInfo]:
    """Every paragraph and table row of the document's body, in order."""
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = Document(BytesIO(data))
    out: list[LineInfo] = []
    for child in doc.element.body.iterchildren():
        if child.tag == f"{_W}p":
            paragraph = Paragraph(child, doc)
            style = paragraph.style.name if paragraph.style is not None else None
            out.append(LineInfo(text=paragraph.text, style=style, bullet=_is_list(paragraph)))
        elif child.tag == f"{_W}tbl":
            for row in Table(child, doc).rows:
                line = _row(row)
                if line is not None:
                    out.append(line)
    return out


def text(lines: list[LineInfo]) -> str:
    return "\n".join(line.text for line in lines)
