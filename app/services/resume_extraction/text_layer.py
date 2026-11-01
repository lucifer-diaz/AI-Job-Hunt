"""[A] The text layer of a PDF, read with PDFium: words with their page, box and font.

PDFium honours /ActualText, which is where producers such as Apple Pages put the right text for a ligature glyph whose
ToUnicode mapping is broken: pypdf reads such a glyph as "Kaga", PDFium reads "Kafka" (docs/resume-extraction-plan.md
§3). A file PDFium cannot open is read with pypdf instead, as before (`source` says which).

Lines are built from the characters' boxes, not from newlines: PDFium emits no newline after a line-break hyphen
(its hyphen character, `FPDFText_IsHyphen`), so "produc-" / "tion" arrives as one run. A line breaks where a character
starts back to the left on a lower baseline; the hyphen ends its line as a soft hyphen (`Line.soft_hyphen`), and
`normalize.join_lines` decides whether the word was broken or really hyphenated.
"""
from __future__ import annotations

import ctypes
import re
from collections import Counter
from io import BytesIO

from app.services.resume_extraction.models import Box, Line, Page, TextLayer, Word

PUA = re.compile("[\ue000-\uf8ff]")
CID = re.compile(r"\(cid:\d+\)")
# Unicode's ligature presentation forms (ﬀ ﬁ ﬂ ﬃ ﬄ ﬅ ﬆ): a reader that kept the glyph instead of its letters.
PRESENTATION = re.compile("[\ufb00-\ufb06]")
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
FLAG_NAMES = ("replacement_char", "private_use", "cid_code", "control_char", "ligature_char", "unmapped_glyph")
_SUBSET = re.compile(r"^[A-Z]{6}\+")


def flags_of(text: str) -> dict[str, int]:
    """Bad characters in extracted text: each is a sign the file's mapping lost information."""
    found = {"replacement_char": text.count("\ufffd"), "private_use": len(PUA.findall(text)),
             "cid_code": len(CID.findall(text)), "control_char": len(CONTROL.findall(text)),
             "ligature_char": len(PRESENTATION.findall(text))}
    return {k: v for k, v in found.items() if v}


def _font(raw, tp, index: int, buf) -> str | None:
    flags = ctypes.c_int()
    if not raw.FPDFText_GetFontInfo(tp, index, buf, len(buf), ctypes.byref(flags)):
        return None
    return _SUBSET.sub("", buf.value.decode("utf-8", "replace")) or None


def _bold(font: str | None, weight: int) -> bool:
    return weight >= 600 or bool(font and re.search(r"bold|black|heavy|semibold|demi", font, re.IGNORECASE))


def _page(raw, pdf_page, number: int, flags: Counter[str]) -> Page:
    width, height = pdf_page.get_size()
    tp = pdf_page.get_textpage()
    page = Page(number=number, width=width, height=height)
    buf = ctypes.create_string_buffer(256)
    line: Line | None = None
    word: list[tuple[str, Box, str | None, bool]] = []
    last: Box | None = None
    band: Box | None = None  # the current line's extent so far: a new line is one whose glyphs leave it

    def end_word() -> None:
        nonlocal word
        if word and line is not None:
            text = "".join(c for c, *_ in word)
            box = word[0][1]
            for _, b, *_ in word[1:]:
                box = box.union(b)
            heights = sorted(b.height for c, b, *_ in word if c.isalnum())
            fonts = Counter(f for _, _, f, _ in word if f)
            font = fonts.most_common(1)[0][0] if fonts else None
            line.words.append(Word(text=text, page=number, box=box, font=font,
                                   size=round(heights[len(heights) // 2], 2) if heights else None,
                                   bold=sum(b for *_, b in word) * 2 > len(word)))
            line.box = box if line.box is None else line.box.union(box)
        word = []

    def end_line() -> None:
        nonlocal line, band
        end_word()
        if line is not None and line.words:
            page.lines.append(line)
        line, band = None, None

    for i in range(tp.count_chars()):
        code = raw.FPDFText_GetUnicode(tp, i)
        ch = chr(code) if code else ""
        if raw.FPDFText_IsHyphen(tp, i):
            if line is not None:
                line.soft_hyphen = True  # TextLayer.text writes it back as "-"
            end_line()
            last = None
            continue
        if ch in ("\r", "\n"):
            end_line()
            last = None
            continue
        if not ch:
            flags["unmapped_glyph"] += 1
            continue
        if ch.isspace():
            end_word()
            continue
        x0, y0, x1, y1 = tp.get_charbox(i)  # pypdfium2 5: (left, bottom, right, top)
        box = Box(x0, y0, x1, y1)
        if band is not None and last is not None:
            overlap = min(box.y1, band.y1) - max(box.y0, band.y0)
            # Off the line's band, or back to the left: a new line without a newline character. Measured against the
            # whole line, so a comma that hangs below the baseline stays on it. (Word breaks come from the space
            # characters PDFium writes or generates; boxes are ink bounds, so a gap between letters is no evidence
            # of a space.)
            if overlap < 0.3 * max(min(box.height, band.height), 0.5) or box.x0 < last.x0 - 1:
                end_line()
        font = _font(raw, tp, i, buf)
        if line is None:
            line = Line(page=number)
        word.append((ch, box, font, _bold(font, raw.FPDFText_GetFontWeight(tp, i))))
        last, band = box, box if band is None else band.union(box)
    end_line()
    tp.close()
    return page


def read_pdfium(data: bytes) -> TextLayer:
    import pypdfium2 as pdfium
    from pypdfium2 import raw

    pdf = pdfium.PdfDocument(data)
    try:
        flags: Counter[str] = Counter()
        pages = [_page(raw, pdf[i], i + 1, flags) for i in range(len(pdf))]
    finally:
        pdf.close()
    layer = TextLayer(source="pdfium", pages=pages)
    layer.flags = {**flags_of(layer.text), **{k: v for k, v in flags.items() if v}}
    return layer


def read_pypdf(data: bytes) -> TextLayer:
    """The old reader, line by line, without positions: the fallback when PDFium cannot open the file."""
    from pypdf import PdfReader

    pages = []
    for number, pdf_page in enumerate(PdfReader(BytesIO(data)).pages, 1):
        box = pdf_page.mediabox
        page = Page(number=number, width=float(box.width), height=float(box.height))
        for text in (pdf_page.extract_text() or "").splitlines():
            if text.strip():
                page.lines.append(Line(page=number, words=[Word(t, number, source="pypdf") for t in text.split()]))
        pages.append(page)
    layer = TextLayer(source="pypdf", pages=pages)
    layer.flags = flags_of(layer.text)
    return layer


def read(data: bytes) -> TextLayer:
    try:
        return read_pdfium(data)
    except Exception:  # noqa: BLE001  (any PDFium failure: the old reader still gets a chance)
        return read_pypdf(data)
