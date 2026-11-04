"""What resume extraction V2 produces, stage by stage. Plain data: no I/O, nothing secret, no model.

Every word keeps where it came from (page, box in PDF points with the origin at the page's bottom left, font, and the
stage that produced it), so a later stage — sections, corrections, the review screen — can point at the original.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Source = Literal["pdfium", "pypdf", "docx", "text"]
Route = Literal["clean", "suspicious", "poor", "no_text"]


@dataclass(frozen=True)
class Box:
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    def union(self, other: Box) -> Box:
        return Box(min(self.x0, other.x0), min(self.y0, other.y0), max(self.x1, other.x1), max(self.y1, other.y1))


@dataclass
class Word:
    text: str
    page: int  # 1-based
    box: Box | None = None
    font: str | None = None  # the embedded font's name without its subset tag ("Calibri-Bold")
    size: float | None = None  # glyph height in points: comparable within one document, not a nominal font size
    bold: bool = False
    source: Source = "pdfium"


@dataclass
class Line:
    page: int
    words: list[Word] = field(default_factory=list)
    box: Box | None = None
    soft_hyphen: bool = False  # the line ends in a hyphen the producer marked as a line-break hyphen

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


@dataclass
class Page:
    number: int
    width: float
    height: float
    lines: list[Line] = field(default_factory=list)


@dataclass
class TextLayer:
    """A document's text as its file states it, before any repair."""

    source: Source
    pages: list[Page]
    flags: dict[str, int] = field(default_factory=dict)  # bad-character counts (FLAG_NAMES), document-wide

    @property
    def text(self) -> str:
        """Lines joined by newlines, pages by a blank line; a soft hyphen stays as "-" at the end of its line."""
        return "\n\n".join("\n".join(line.text + ("-" if line.soft_hyphen else "") for line in page.lines)
                           for page in self.pages)


@dataclass
class LineInfo:
    """One line of the extracted text with what the file says about its look (all None for a text file)."""

    text: str
    size: float | None = None  # median glyph height of its words
    bold: bool = False
    x0: float | None = None  # left edge, PDF points
    style: str | None = None  # a Word paragraph style ("Heading 1", "List Bullet")
    bullet: bool = False  # a list item whose marker is not a character (a drawn bullet, Word numbering)
    segments: list[str] | None = None  # the line's words split where the page leaves a column gap


@dataclass
class PageQuality:
    number: int
    chars: int
    words: int
    lowercase_words: int
    oov_words: int
    suspect_words: int


@dataclass
class QualityReport:
    """How far the text layer can be trusted, from checks code can make. Counts only: never the words themselves."""

    route: Route
    reasons: list[str]
    chars: int
    words: int
    oov_rate: float | None  # unknown lower-case words / lower-case words; None without any
    suspect_words: int  # words with an odd character inside lower-case letters ("architecYng")
    flags: dict[str, int]
    pages: list[PageQuality]

    def summary(self) -> dict[str, object]:
        return {"route": self.route, "reasons": self.reasons, "chars": self.chars, "words": self.words,
                "oov_rate": self.oov_rate, "suspect_words": self.suspect_words, "flags": self.flags,
                "pages": len(self.pages)}


@dataclass
class Extraction:
    text: str  # repaired text, raw in document_text's sense: callers run clean_raw_text, then it is Resume.raw_text
    text_layer: str  # the file's own text before any repair (TextLayer.text), for evaluation and review
    quality: QualityReport
    layer: TextLayer | None = None  # None for formats without layout (TXT)
    sections: list[dict[str, object]] = field(default_factory=list)  # sections.Heading.as_dict(), in order
    view: str = ""  # structure.view: the cleaned text with its structure marked, for the parser
    lines: list[LineInfo] = field(default_factory=list)  # the cleaned text's lines with their look
    corrections: list[dict[str, object]] = field(default_factory=list)  # correction.Correction.as_dict()
    ocr: str | None = None  # the OCR engine that read the pages, when one did

    @property
    def clean_text(self) -> str:
        """The text with its corrections applied (raw in document_text's sense: callers run clean_raw_text)."""
        from app.services.resume_extraction.correction import apply

        return apply(self.text, self.corrections) if self.corrections else self.text
