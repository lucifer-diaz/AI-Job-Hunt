"""Build the resume_extraction evaluation documents from the hand-written resumes (tests/fixtures/resume_extraction).

    python scripts/build_resume_fixtures.py            # rebuild every document and the dataset's document examples
    python scripts/build_resume_fixtures.py --check    # only verify the committed documents match the dataset

Reproduces the PDF bug the suite measures (docs/resume-extraction-plan.md §3) without any real resume:

    1. a font with real ligature glyphs: Inter (SIL OFL 1.1), subset to Latin, renamed "Ligature Fixture Sans", with
       composite glyphs t_i t_f f_k f_t t_t f_i f_l and a `liga` feature (fontTools)
    2. each resume rendered to HTML in its layout and printed to PDF by Chromium (Playwright), which maps every
       ligature glyph correctly ("ti")
    3. three variants of each PDF:
         clean        as printed
         broken       each ligature glyph's ToUnicode entry rewritten to one wrong character (CONFUSIONS), as a
                      font with a broken mapping does: "Kafka" reads "Kaga", "Software" reads "So4ware"
         actualtext   broken, plus /ActualText marked content around every ligature glyph carrying the right text,
                      as Apple Pages / Quartz writes it
    4. DOCX (python-docx; one with its skills in a table) and TXT copies

Bullets are drawn as shapes (Chromium's list markers: no character in the text layer) in the single and sidebar
layouts, and as a "•" character in the two-column and table layouts, since real producers do both.

Needs fontTools (`pip install fonttools`, a dev dependency) and Inter's TTFs (INTER_FONT_DIR, default
~/Library/Fonts). The documents are committed, so the suite never needs either. Ground truth is never generated:
the builder only renders tests/fixtures/resume_extraction/resumes.json, which a person writes.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.agent_eval.resume_extraction import (
    role_line,
    section_lines,
)

FIXTURES = ROOT / "tests" / "fixtures" / "resume_extraction"
DOCUMENTS = FIXTURES / "documents"
DATASET = FIXTURES / "dataset.json"
PDF, DOCX, TXT = ("application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                  "text/plain")

LIGATURES = ("ti", "tf", "fk", "ft", "tt", "fi", "fl")
# What a broken ToUnicode entry makes each ligature read as, by weight. Taken from the real Calibri PDF: "ti" read Y
# in the regular face and Z in the bold one, "tf" [ and B, "fk" g, "ft" 4, "tt" C; fi / fl came out as the
# Unicode ligature characters.
CONFUSIONS = {"regular": {"ti": "Y", "tf": "[", "fk": "g", "ft": "4", "tt": "C", "fi": "ﬁ", "fl": "ﬂ"},
              "bold": {"ti": "Z", "tf": "B", "fk": "g", "ft": "4", "tt": "C", "fi": "ﬁ", "fl": "ﬂ"}}
LAYOUTS = {"ana": "single", "ben": "two_column", "chloe": "sidebar", "dev": "table"}
VARIANTS = ("clean", "broken", "actualtext")
EXTRA = (("ana", DOCX), ("dev", DOCX), ("ben", TXT), ("chloe", TXT))


# ───────────── 1. font ─────────────

def build_font(src: Path, out: Path, style: str) -> None:
    from fontTools.feaLib.builder import addOpenTypeFeaturesFromString
    from fontTools.subset import Options, Subsetter
    from fontTools.ttLib import TTFont
    from fontTools.ttLib.tables._g_l_y_f import Glyph, GlyphComponent

    font = TTFont(src)
    options = Options()
    options.layout_features, options.name_IDs, options.glyph_names = [], ["*"], True
    subsetter = Subsetter(options)
    subsetter.populate(unicodes=[*range(0x20, 0x7F), 0xA0, 0xB7, 0x2013, 0x2014, 0x2019, 0x2022])
    subsetter.subset(font)
    glyf, hmtx, cmap = font["glyf"], font["hmtx"], font.getBestCmap()
    for lig in LIGATURES:
        first, second = cmap[ord(lig[0])], cmap[ord(lig[1])]
        glyph, x = Glyph(), 0
        glyph.numberOfContours, glyph.components = -1, []
        for part in (first, second):
            component = GlyphComponent()
            component.glyphName, component.x, component.y = part, x, 0
            component.flags = 0x4 if part == first else 0  # USE_MY_METRICS on the first
            glyph.components.append(component)
            x += hmtx[part][0]
        glyf["_".join(lig)] = glyph
        hmtx["_".join(lig)] = (x, hmtx[first][1])
    font.setGlyphOrder(list(glyf.glyphOrder))
    if "GSUB" in font:
        del font["GSUB"]
    rules = "".join(f"  sub {cmap[ord(lig[0])]} {cmap[ord(lig[1])]} by {'_'.join(lig)};\n" for lig in LIGATURES)
    addOpenTypeFeaturesFromString(font, f"languagesystem DFLT dflt;\nlanguagesystem latn dflt;\n"
                                        f"feature liga {{\n{rules}}} liga;\n")
    family = "Ligature Fixture Sans"
    for record in font["name"].names:
        if record.nameID in (1, 16):
            record.string = family
        elif record.nameID in (2, 17):
            record.string = style
        elif record.nameID == 4:
            record.string = f"{family} {style}"
        elif record.nameID == 6:
            record.string = f"LigatureFixtureSans-{style}"
    font.save(out)


# ───────────── 2. HTML → PDF ─────────────

CSS = """
@font-face { font-family: Lig; src: url("{regular}"); font-weight: 400; }
@font-face { font-family: Lig; src: url("{bold}"); font-weight: 700; }
@page { size: Letter; margin: 0.6in; }
body { font-family: Lig; font-size: 10.5pt; line-height: 1.35; font-variant-ligatures: common-ligatures; color: #111; }
h1 { font-size: 20pt; margin: 0; } h2 { font-size: 12pt; margin: 14px 0 4px; text-transform: none; }
.headline { font-size: 12pt; margin: 2px 0; } .contact { margin: 2px 0 8px; }
ul { margin: 2px 0 6px 18px; padding: 0; } li { margin: 1px 0; } p { margin: 2px 0; }
.role { margin-top: 6px; }
.two_column .body { column-count: 2; column-gap: 28px; }
.sidebar .body { display: flex; gap: 24px; } .sidebar aside { width: 32%; background: #eef2f6; padding: 8px; }
.sidebar main { width: 68%; }
table { border-collapse: collapse; width: 100%; } td { vertical-align: top; padding: 1px 6px 1px 0; }
td.dates { text-align: right; white-space: nowrap; }
.two_column ul, .table ul { list-style: none; margin-left: 4px; }
.two_column li::before, .table li::before { content: "\\2022  "; }
"""


def _e(text: str) -> str:
    return html.escape(text, quote=False)


def _skills_html(resume: dict, layout: str) -> str:
    if layout == "table":
        rows = "".join(f"<tr><td><b>{_e(g['category'])}</b></td><td>{_e(', '.join(i['name'] for i in g['items']))}"
                       f"</td></tr>" for g in resume["skills"])
        return f"<table>{rows}</table>"
    out = []
    for group in resume["skills"]:
        label = f"<b>{_e(group['category'])}:</b> " if group["category"] else ""
        out.append(f"<p>{label}{_e(', '.join(i['name'] for i in group['items']))}</p>")
    return "".join(out)


def _role_html(role: dict, layout: str) -> str:
    bullets = "".join(f"<li>{_e(b)}</li>" for b in role["bullets"])
    if layout == "table":
        header = (f"<table><tr><td><b>{_e(role['title'])}</b> | {_e(role['company'])} | {_e(role['location'])}</td>"
                  f"<td class='dates'>{_e(role['dates'])}</td></tr></table>")
    else:
        header = (f"<p><b>{_e(role['title'])}</b> | {_e(role['company'])} | {_e(role['location'])} | "
                  f"{_e(role['dates'])}</p>")
    return f"<div class='role'>{header}<ul>{bullets}</ul></div>"


def _section_html(resume: dict, kind: str, layout: str) -> str:
    title = f"<h2>{_e(resume['headings'][kind])}</h2>"
    if kind == "summary":
        return f"{title}<p>{_e(resume['summary'])}</p>"
    if kind == "skills":
        return title + _skills_html(resume, layout)
    if kind == "experience":
        return title + "".join(_role_html(r, layout) for r in resume["experience"])
    if kind == "education":
        return title + "".join(f"<p>{_e(e['degree'])} | {_e(e['institution'])} | {_e(e['dates'])}</p>"
                               for e in resume["education"])
    if kind == "certifications":
        return title + "".join(f"<p>{_e(c['name'])} | {_e(c['issuer'])} | {_e(c['date'])}</p>"
                               for c in resume["certifications"])
    if kind == "projects":
        return title + "".join(f"<p><b>{_e(p['name'])}:</b> {_e(p['description'])}</p>" for p in resume["projects"])
    raise ValueError(kind)


def render_html(resume: dict, layout: str, fonts: dict[str, str]) -> str:
    head = (f"<h1>{_e(resume['name'])}</h1><p class='headline'>{_e(resume['headline'])}</p>"
            f"<p class='contact'>{_e(' | '.join(resume['contact']))}</p>")
    if layout == "sidebar":
        side = [k for k in resume["order"] if k in ("skills", "education")]
        main = [k for k in resume["order"] if k not in side]
        body = (f"<div class='body'><aside>{''.join(_section_html(resume, k, layout) for k in side)}</aside>"
                f"<main>{''.join(_section_html(resume, k, layout) for k in main)}</main></div>")
    else:
        body = f"<div class='body'>{''.join(_section_html(resume, k, layout) for k in resume['order'])}</div>"
    css = CSS.replace("{regular}", fonts["regular"]).replace("{bold}", fonts["bold"])
    return (f"<!doctype html><html><head><meta charset='utf-8'><style>{css}</style></head>"
            f"<body class='{layout}'>{head}{body}</body></html>")


def print_pdfs(jobs: list[tuple[str, Path]]) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        for html_path, out in jobs:
            page.goto(Path(html_path).as_uri())
            page.evaluate("document.fonts.ready")
            page.pdf(path=str(out), format="Letter", print_background=True,
                     margin={"top": "0.6in", "bottom": "0.6in", "left": "0.6in", "right": "0.6in"})
        browser.close()


# ───────────── 3. break the ligature mappings ─────────────

_BFCHAR = re.compile(rb"<([0-9A-Fa-f]{4})>\s*<([0-9A-Fa-f]{8,})>")


def _ligature_codes(cmap: bytes) -> dict[bytes, str]:
    """Glyph code → ligature text, for every ToUnicode entry that maps one code to a ligature."""
    out = {}
    for code, dest in _BFCHAR.findall(cmap):
        text = bytes.fromhex(dest.decode()).decode("utf-16-be")
        if text in LIGATURES:
            out[bytes.fromhex(code.decode())] = text
    return out


def break_pdf(src: Path, out: Path, *, actual_text: bool) -> dict[str, int]:
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import (
        ArrayObject,
        ByteStringObject,
        ContentStream,
        DictionaryObject,
        FloatObject,
        NameObject,
        NumberObject,
        TextStringObject,
    )

    writer = PdfWriter(clone_from=PdfReader(src))
    counts: dict[str, int] = {}
    for page in writer.pages:
        fonts = page["/Resources"]["/Font"]
        codes: dict[str, dict[bytes, str]] = {}
        for name, ref in fonts.items():
            font = ref.get_object()
            weight = "bold" if "Bold" in str(font.get("/BaseFont", "")) else "regular"
            stream = font["/ToUnicode"].get_object()
            cmap = stream.get_data()
            found = _ligature_codes(cmap)
            codes[name] = found

            def swap(match: re.Match, weight: str = weight) -> bytes:
                text = bytes.fromhex(match.group(2).decode()).decode("utf-16-be")
                if text not in LIGATURES:
                    return match.group(0)
                wrong = CONFUSIONS[weight][text].encode("utf-16-be").hex().upper().encode()
                return b"<" + match.group(1) + b"> <" + wrong + b">"
            stream.set_data(_BFCHAR.sub(swap, cmap))
        content = ContentStream(page.get_contents(), writer)
        ops, current = [], None
        for operands, operator in content.operations:
            if operator == b"Tf":
                current = operands[0]
            if operator not in (b"Tj", b"TJ"):
                ops.append((operands, operator))
                continue
            items = operands[0] if operator == b"TJ" else [operands[0]]
            ligs = codes.get(current, {})
            for item in items:
                if isinstance(item, (NumberObject, FloatObject)):  # a TJ position adjustment
                    ops.append(([ArrayObject([item])], b"TJ"))
                    continue
                data = item.original_bytes if hasattr(item, "original_bytes") else bytes(item)
                run = b""
                for i in range(0, len(data), 2):
                    code = data[i:i + 2]
                    if code in ligs:
                        counts[ligs[code]] = counts.get(ligs[code], 0) + 1
                        if actual_text:
                            if run:
                                ops.append(([ByteStringObject(run)], b"Tj"))
                                run = b""
                            props = DictionaryObject({NameObject("/ActualText"): TextStringObject(ligs[code])})
                            ops += [([NameObject("/Span"), props], b"BDC"), ([ByteStringObject(code)], b"Tj"),
                                    ([], b"EMC")]
                            continue
                    run += code
                if run:
                    ops.append(([ByteStringObject(run)], b"Tj"))
        content.operations = ops
        page.replace_contents(content)
    writer.add_metadata({"/Producer": "Job Hunt OS fixture builder (Chromium print, ligature ToUnicode broken"
                                      f"{', ActualText kept' if actual_text else ''})"})
    with open(out, "wb") as f:
        writer.write(f)
    return counts


# ───────────── 4. DOCX / TXT ─────────────

def write_txt(resume: dict, out: Path) -> None:
    lines = [resume["name"], resume["headline"], " | ".join(resume["contact"])]
    for kind in resume["order"]:
        lines += ["", resume["headings"][kind]]
        if kind == "experience":
            for r in resume["experience"]:
                lines += [role_line(r), *(f"• {b}" for b in r["bullets"]), ""]
        else:
            lines += section_lines(resume, kind)
    out.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def write_docx(resume: dict, out: Path, *, skills_table: bool) -> None:
    from docx import Document

    doc = Document()
    doc.add_heading(resume["name"], level=0)
    doc.add_paragraph(resume["headline"])
    doc.add_paragraph(" | ".join(resume["contact"]))
    for kind in resume["order"]:
        doc.add_heading(resume["headings"][kind], level=1)
        if kind == "experience":
            for r in resume["experience"]:
                doc.add_paragraph(role_line(r))
                for b in r["bullets"]:
                    doc.add_paragraph(b, style="List Bullet")
        elif kind == "skills" and skills_table:
            table = doc.add_table(rows=0, cols=2)
            for g in resume["skills"]:
                cells = table.add_row().cells
                cells[0].text, cells[1].text = g["category"], ", ".join(i["name"] for i in g["items"])
        else:
            for line in section_lines(resume, kind):
                doc.add_paragraph(line)
    doc.core_properties.author = "Job Hunt OS fixture builder"
    doc.save(out)


# ───────────── dataset ─────────────

def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def document_examples() -> list[dict]:
    out = []
    for key, layout in LAYOUTS.items():
        for variant in VARIANTS:
            out.append({"key": f"{key}-{layout}-{variant}", "file": f"{key}-{layout}-{variant}.pdf", "kind": PDF,
                        "resume": key, "metadata": {"layout": layout, "variant": variant, "format": "pdf"}})
    for key, kind in EXTRA:
        ext = "docx" if kind == DOCX else "txt"
        layout = "skills_table" if key == "dev" and kind == DOCX else "plain"
        out.append({"key": f"{key}-{ext}", "file": f"{key}.{ext}", "kind": kind, "resume": key,
                    "metadata": {"layout": layout, "variant": "clean", "format": ext}})
    return out


def write_dataset(examples: list[dict]) -> None:
    data = json.loads(DATASET.read_text())
    text_cases = [e for e in data["examples"] if e.get("metadata", {}).get("format") == "text"]
    docs = [{"key": e["key"], "inputs": {"document": e["file"], "kind": e["kind"],
                                         "sha256": _sha(DOCUMENTS / e["file"])},
             "outputs": {"resume": e["resume"]}, "metadata": e["metadata"]} for e in examples]
    data["examples"] = docs + text_cases
    data["fixture"]["resumes_sha256"] = _sha(FIXTURES / "resumes.json")
    DATASET.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")


def check() -> int:
    data = json.loads(DATASET.read_text())
    bad = [e["key"] for e in data["examples"] if "document" in e["inputs"]
           and (not (DOCUMENTS / e["inputs"]["document"]).exists()
                or _sha(DOCUMENTS / e["inputs"]["document"]) != e["inputs"]["sha256"])]
    if data["fixture"].get("resumes_sha256") != _sha(FIXTURES / "resumes.json"):
        bad.append("resumes.json changed: rebuild the documents")
    print("\n".join(bad) or "documents match the dataset")
    return 1 if bad else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        return check()
    inter = Path(os.environ.get("INTER_FONT_DIR", Path.home() / "Library" / "Fonts"))
    resumes = json.loads((FIXTURES / "resumes.json").read_text())["resumes"]
    DOCUMENTS.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        fonts = {}
        for style in ("Regular", "Bold"):
            out = work / f"LigatureFixtureSans-{style}.ttf"
            build_font(inter / f"Inter-{style}.ttf", out, style)
            fonts[style.lower()] = out.as_uri()
        jobs = []
        for key, layout in LAYOUTS.items():
            page = work / f"{key}.html"
            page.write_text(render_html(resumes[key], layout, fonts), encoding="utf-8")
            jobs.append((str(page), DOCUMENTS / f"{key}-{layout}-clean.pdf"))
        print_pdfs(jobs)
        for key, layout in LAYOUTS.items():
            clean = DOCUMENTS / f"{key}-{layout}-clean.pdf"
            counts = break_pdf(clean, DOCUMENTS / f"{key}-{layout}-broken.pdf", actual_text=False)
            break_pdf(clean, DOCUMENTS / f"{key}-{layout}-actualtext.pdf", actual_text=True)
            missing = {"ti", "fk", "ft"} - set(counts)
            if key == "ana" and missing:
                raise SystemExit(f"the font produced no {sorted(missing)} ligature glyphs: check the liga feature")
            print(f"{key}: ligature glyphs {dict(sorted(counts.items()))}")
    for key, kind in EXTRA:
        if kind == DOCX:
            write_docx(resumes[key], DOCUMENTS / f"{key}.docx", skills_table=key == "dev")
        else:
            write_txt(resumes[key], DOCUMENTS / f"{key}.txt")
    write_dataset(document_examples())
    return check()


if __name__ == "__main__":
    raise SystemExit(main())
