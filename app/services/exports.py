from __future__ import annotations

from io import BytesIO

from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import simpleSplit
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

from app.services.resume_format import body_text, to_plain

MARGIN = 48
BODY, BOLD = "Helvetica", "Helvetica-Bold"
INK, MUTED, RULE = (0.07, 0.08, 0.15), (0.35, 0.38, 0.45), (0.8, 0.82, 0.87)


def markdown_to_simple_pdf(markdown: str) -> bytes:
    """Render the resume Markdown dialect (# / ## / ### Title | Company | Location | Dates / - bullets)."""
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    usable = width - 2 * MARGIN
    y = height - MARGIN
    after_name = False

    def room(needed: float) -> None:
        nonlocal y
        if y - needed < MARGIN:
            c.showPage()
            y = height - MARGIN

    def paragraph(text: str, font: str = BODY, size: float = 9.5, indent: float = 0, color=INK,
                  leading: float | None = None, bullet: bool = False) -> None:
        nonlocal y
        leading = leading or size * 1.35
        lines = simpleSplit(text, font, size, usable - indent) or [""]
        for i, line in enumerate(lines):
            room(leading)
            c.setFillColorRGB(*color)
            c.setFont(font, size)
            if bullet and i == 0:
                c.drawString(MARGIN + indent - 9, y - size, "•")
            c.drawString(MARGIN + indent, y - size, line)
            y -= leading

    for raw in body_text(markdown).splitlines():
        line = to_plain(raw.rstrip()).replace("**", "")
        stripped = line.strip()
        if not stripped:
            y -= 4
            continue
        if stripped.startswith("### "):
            parts = [p.strip() for p in stripped[4:].split("|")]
            dates = parts[-1] if len(parts) > 2 else ""
            title = parts[0]
            rest = " | ".join(parts[1:-1] if dates else parts[1:])
            y -= 3
            size = 10.5
            left_width = stringWidth(title, BOLD, size) + (stringWidth(f"  {rest}", BODY, 9.5) if rest else 0)
            dates_width = stringWidth(dates, BODY, 9.5) if dates else 0
            if left_width + dates_width + 12 <= usable:
                room(size * 1.4)
                c.setFillColorRGB(*INK)
                c.setFont(BOLD, size)
                c.drawString(MARGIN, y - size, title)
                if rest:
                    c.setFont(BODY, 9.5)
                    c.setFillColorRGB(*MUTED)
                    c.drawString(MARGIN + stringWidth(title, BOLD, size), y - size, f"  {rest}")
                if dates:
                    c.setFont(BODY, 9.5)
                    c.setFillColorRGB(*MUTED)
                    c.drawRightString(width - MARGIN, y - size, dates)
                y -= size * 1.4
            else:
                paragraph(stripped[4:], BOLD, size)
            continue
        if stripped.startswith("## "):
            y -= 8
            room(26)
            paragraph(stripped[3:].upper(), BOLD, 10.5, color=INK, leading=14)
            c.setStrokeColorRGB(*RULE)
            c.setLineWidth(0.6)
            c.line(MARGIN, y + 2, width - MARGIN, y + 2)
            y -= 4
            after_name = False
            continue
        if stripped.startswith("# "):
            paragraph(stripped[2:], BOLD, 18, leading=24)
            after_name = True
            continue
        if stripped.startswith("- "):
            paragraph(stripped[2:], indent=12, bullet=True)
            continue
        paragraph(stripped, color=MUTED if after_name else INK)
    c.save()
    buffer.seek(0)
    return buffer.read()
