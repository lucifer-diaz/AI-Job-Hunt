from __future__ import annotations

from io import BytesIO
from textwrap import wrap

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas


def markdown_to_simple_pdf(markdown: str) -> bytes:
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    x, y = 48, height - 48
    for raw_line in markdown.splitlines():
        line = raw_line.replace("# ", "").replace("## ", "").replace("**", "")
        if raw_line.startswith("# "):
            c.setFont("Helvetica-Bold", 16)
        elif raw_line.startswith("## "):
            c.setFont("Helvetica-Bold", 12)
        else:
            c.setFont("Helvetica", 9.5)
        chunks = wrap(line, width=105) or [""]
        for chunk in chunks:
            if y < 48:
                c.showPage()
                y = height - 48
                c.setFont("Helvetica", 9.5)
            c.drawString(x, y, chunk)
            y -= 13
        if not line:
            y -= 5
    c.save()
    buffer.seek(0)
    return buffer.read()
