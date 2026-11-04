"""[B] OCR of a PDF's rendered pages: what the page *shows*, as a second reading of words the text layer may have
mapped wrong. The rendered page is right even when a font's ToUnicode map is not (a ligature drawn as "fk" reads
"Kafka" on the page and "Kaga" in the text layer).

    render   each page at RENDER_DPI with PDFium (the same library that reads the text layer)
    read     a pluggable backend: `ocr(image) -> [OcrWord]`; Tesseract by default, through its own binary on this
             machine. No external service ever receives a resume page: a VLM backend would be a setting, off.

Runs only when the quality report flags the text layer, and is bounded (`RESUME_OCR_MAX_PAGES`,
`RESUME_OCR_TIMEOUT_SECONDS`): a slow or missing engine means no OCR, never a failed upload.
"""
from __future__ import annotations

import csv
import io
import logging
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Protocol

from app.services.resume_extraction.models import Box

log = logging.getLogger("jobhunt.resume_extraction")

RENDER_DPI = 300
MIN_CONFIDENCE = 60.0  # Tesseract's own 0-100 word confidence


@dataclass(frozen=True)
class OcrWord:
    text: str
    page: int  # 1-based
    box: Box  # PDF points, origin at the page's bottom left (as the text layer)
    conf: float


class Backend(Protocol):
    name: str

    def available(self) -> bool: ...

    def read(self, png: bytes, timeout: float) -> list[tuple[str, float, float, float, float, float]]:
        """Words on an image as (text, left, top, width, height, confidence) in pixels."""


class Tesseract:
    name = "tesseract"

    def available(self) -> bool:
        return shutil.which("tesseract") is not None

    def read(self, png: bytes, timeout: float) -> list[tuple[str, float, float, float, float, float]]:
        out = subprocess.run(["tesseract", "stdin", "stdout", "-l", "eng", "--psm", "3", "tsv"], input=png,
                             capture_output=True, timeout=timeout, check=True)
        rows = csv.DictReader(io.StringIO(out.stdout.decode("utf-8", "replace")), delimiter="\t",
                              quoting=csv.QUOTE_NONE)
        words = []
        for row in rows:
            text = (row.get("text") or "").strip()
            if row.get("level") == "5" and text:
                words.append((text, float(row["left"]), float(row["top"]), float(row["width"]), float(row["height"]),
                              float(row["conf"])))
        return words


BACKENDS: dict[str, Backend] = {"tesseract": Tesseract()}


def backend() -> Backend | None:
    from app.settings import get_settings

    settings = get_settings()
    if not settings.resume_ocr_enabled:
        return None
    engine = BACKENDS.get(settings.resume_ocr_backend)
    return engine if engine is not None and engine.available() else None


def read_pdf(data: bytes, engine: Backend | None = None, *, max_pages: int | None = None,
             timeout: float | None = None) -> list[OcrWord] | None:
    """The words the rendered pages show, in PDF points. None when no OCR ran (none available, a failure, or out of
    time); a partial result is never returned as a whole one."""
    import pypdfium2 as pdfium

    from app.settings import get_settings

    settings = get_settings()
    engine = engine or backend()
    if engine is None:
        return None
    max_pages = max_pages if max_pages is not None else settings.resume_ocr_max_pages
    budget = timeout if timeout is not None else float(settings.resume_ocr_timeout_seconds)
    deadline = time.monotonic() + budget
    scale = RENDER_DPI / 72.0
    out: list[OcrWord] = []
    try:
        pdf = pdfium.PdfDocument(data)
        try:
            if len(pdf) > max_pages:
                log.info("resume OCR skipped: %d pages (limit %d)", len(pdf), max_pages)
                return None
            for index in range(len(pdf)):
                page = pdf[index]
                _, height = page.get_size()
                buf = io.BytesIO()
                page.render(scale=scale).to_pil().save(buf, "PNG")
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError
                for text, x, y, w, h, conf in engine.read(buf.getvalue(), left):
                    if conf < MIN_CONFIDENCE:
                        continue
                    out.append(OcrWord(text=text, page=index + 1, conf=conf,
                                       box=Box(x / scale, height - (y + h) / scale, (x + w) / scale, height - y / scale)))
        finally:
            pdf.close()
    except (TimeoutError, subprocess.TimeoutExpired):
        log.info("resume OCR stopped: over %.0fs", budget)
        return None
    except Exception:
        log.warning("resume OCR failed", exc_info=True)
        return None
    return out
