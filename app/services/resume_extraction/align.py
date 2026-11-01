"""[C] Which OCR words sit where each text-layer word is drawn: the page's reading of that word.

Both readings are boxes in PDF points on the same page. A layer word takes the OCR words whose boxes it overlaps by at
least half of the smaller box (a tokenization difference — "RAG/Kafka" as one word or two — joins them, left to
right). Deterministic; no model.
"""
from __future__ import annotations

from app.services.resume_extraction.models import Box, TextLayer
from app.services.resume_extraction.ocr import OcrWord

MIN_OVERLAP = 0.5


def _overlap(a: Box, b: Box) -> float:
    w = min(a.x1, b.x1) - max(a.x0, b.x0)
    h = min(a.y1, b.y1) - max(a.y0, b.y0)
    if w <= 0 or h <= 0:
        return 0.0
    smaller = min((a.x1 - a.x0) * (a.y1 - a.y0), (b.x1 - b.x0) * (b.y1 - b.y0)) or 1.0
    return (w * h) / smaller


def readings(layer: TextLayer, words: list[OcrWord]) -> list[tuple[str, str]]:
    """(layer word, what the page shows there) for every layer word OCR saw."""
    by_page: dict[int, list[OcrWord]] = {}
    for word in words:
        by_page.setdefault(word.page, []).append(word)
    out = []
    for page in layer.pages:
        candidates = by_page.get(page.number, [])
        for line in page.lines:
            for word in line.words:
                if word.box is None:
                    continue
                seen = sorted((w for w in candidates if _overlap(word.box, w.box) >= MIN_OVERLAP),
                              key=lambda w: w.box.x0)
                if seen:
                    out.append((word.text, "".join(w.text for w in seen)))
    return out
