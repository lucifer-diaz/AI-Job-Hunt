"""The resume_extraction suite: how faithfully a pipeline turns an uploaded resume into text and structure.

    documents   tests/fixtures/resume_extraction/documents (scripts/build_resume_fixtures.py renders the hand-written
                resumes.json: PDF in four layouts × clean / ligature mapping broken / broken with /ActualText, DOCX, TXT)
    text cases  hand-written text-layer corruptions in dataset.json: the repair step alone
    real        your own anonymised resumes in tests/fixtures/resume_extraction/real/ (gitignored), only when asked
                (`--with-real-resumes`); never committed

A pipeline is three functions (`Pipeline`): read a document (its raw text layer and its final text), repair a text
layer, and structure a text; optionally a fourth that routes it (the quality report). `current` is exactly what an
upload does today: pypdf + text_repair.repair_glyphs + clean_raw_text, then structured_resume.parse_deterministic
(the AI parser needs a model and is scored separately). `v2` is RESUME_EXTRACTION_V2 (app.services.resume_extraction).

Metrics, all computed against the hand-written ground truth, never against another extractor:

    cer                   character errors / expected characters, over tokens (layout order does not count here)
    valid_words_altered   words the text layer had right that the final text no longer has (hard gate 0)
    correction_precision  of the changes from text layer to final text, those that produce expected words
    field_precision/recall structured facts (skills, roles, dates, bullets, education, certifications, projects,
                          summary) against the ground truth
    misfiled              expected facts missing from their section but present in another (a skills block parsed
                          as summary text)
    hallucinated_values   structured values found neither in the ground truth nor in the extracted text: invented,
                          not misread (hard gate 0)
    unrecognized_dropped  terms no lexicon knows (recognized: false) that vanish from the structure entirely (hard 0)
    bullets_intact        experience bullets that come out whole, in order, in the final text
"""
from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from app.services.agent_eval.datasets import FIXTURES, Dataset
from app.services.agent_eval.suites import SuiteResult, _rate

ROOT = FIXTURES / "resume_extraction"
DOCUMENTS = ROOT / "documents"
REAL = ROOT / "real"


# ───────────── pipelines ─────────────

@dataclass(frozen=True)
class Pipeline:
    read: Callable[[str, bytes], tuple[str, str, Any]]  # (kind, bytes) → (raw text layer, final text, context)
    repair: Callable[[str], str]  # raw text layer → final text
    structure: Callable[[str, Any], dict[str, Any]]  # (final text, context) → StructuredResumeIn-shaped data
    route: Callable[[str, bytes], str] | None = None  # (kind, bytes) → quality route (clean / suspicious / poor)


def _current_read(kind: str, data: bytes) -> tuple[str, str, Any]:
    from io import BytesIO

    from app.services import document_text
    from app.services.structured_resume import clean_raw_text

    if kind == document_text.PDF:
        from pypdf import PdfReader

        layer = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(data)).pages)
    else:
        layer = document_text.extract(kind, data)
    return layer, clean_raw_text(document_text.extract(kind, data)), None


def _current_repair(text: str) -> str:
    from app.services.structured_resume import clean_raw_text
    from app.services.text_repair import repair_glyphs

    return clean_raw_text(repair_glyphs(text))


def _current_structure(text: str, context: Any = None) -> dict[str, Any]:
    from app.services.structured_resume import parse_deterministic

    return parse_deterministic(text)


def _v2_read(kind: str, data: bytes) -> tuple[str, str, Any]:
    from app.services.resume_extraction import pipeline
    from app.services.structured_resume import clean_raw_text

    out = pipeline.extract(kind, data)
    return out.text_layer, clean_raw_text(out.clean_text), out


def _v2_structure(text: str, context: Any = None) -> dict[str, Any]:
    from app.services.resume_extraction import pipeline
    from app.services.structured_resume import parse_deterministic

    return parse_deterministic(context.view if context is not None else pipeline.parse_view(text), marked=True)


def _v2_repair(text: str) -> str:
    """A text layer alone (no page to read): normalisation, glyph repair, then the consistent-rule corrections."""
    from app.services.resume_extraction import correction
    from app.services.resume_extraction.normalize import normalize
    from app.services.structured_resume import clean_raw_text
    from app.services.text_repair import repair_glyphs

    repaired = repair_glyphs(normalize(text))
    return clean_raw_text(correction.apply(repaired, correction.correct(repaired)))


def _v2_route(kind: str, data: bytes) -> str:
    from app.services.resume_extraction import pipeline

    return pipeline.extract(kind, data).quality.route


PIPELINES: dict[str, Pipeline] = {
    "current": Pipeline(_current_read, _current_repair, _current_structure),
    "v2": Pipeline(_v2_read, _v2_repair, _v2_structure, _v2_route),
}
# The quality route each fixture variant should get: the file's mapping is broken, or it is not.
EXPECTED_ROUTE = {"clean": ("clean",), "actualtext": ("clean",), "broken": ("suspicious", "poor")}


# ───────────── ground truth ─────────────

def role_line(role: dict[str, Any]) -> str:
    return f"{role['title']} | {role['company']} | {role['location']} | {role['dates']}"


def role_lines(role: dict[str, Any]) -> list[str]:
    """A role's header as the document writes it: `header` when the ground truth records it (a real layout that names
    the company once above several roles), else the one line the fixture builder renders."""
    return list(role.get("header") or [role_line(role)])


def section_lines(resume: dict[str, Any], kind: str) -> list[str]:
    """The lines a section is written as (the builder renders exactly these words)."""
    if kind == "summary":
        return [resume["summary"]]
    if kind == "skills":
        return [f"{g['category']}: " * bool(g["category"]) + ", ".join(i["name"] for i in g["items"])
                for g in resume["skills"]]
    if kind == "experience":
        return [line for r in resume["experience"] for line in (*role_lines(r), *r["bullets"])]
    if kind == "education":
        return [f"{e['degree']}{', ' + e['field_of_study'] if e.get('field_of_study') else ''} | {e['institution']} | "
                f"{e['dates']}" for e in resume["education"]]
    if kind == "certifications":
        return [f"{c['name']} | {c['issuer']} | {c['date']}" for c in resume["certifications"]]
    if kind == "projects":
        return [f"{p['name']}: {p['description']}" for p in resume["projects"]]
    raise ValueError(kind)


def document_lines(resume: dict[str, Any]) -> list[str]:
    """Every line of the document. `other_lines` holds text no field does (a company's overall dates above its roles,
    a degree's location): real layouts have it, and the words it holds are part of the text to extract."""
    lines = [resume["name"], resume["headline"], " | ".join(resume["contact"])]
    for kind in resume["order"]:
        lines += [resume["headings"][kind], *section_lines(resume, kind)]
    return lines + list(resume.get("other_lines") or [])


def load_resumes() -> dict[str, Any]:
    return json.loads((ROOT / "resumes.json").read_text())["resumes"]


# ───────────── text metrics ─────────────

_EDGE = ".,;:()\"'“”‘’"


def tokens(text: str) -> list[str]:
    """Words as a reader sees them: split on white space, bullets and column bars, outer punctuation removed."""
    out = []
    for raw in re.split(r"[\s|•·]+", text or ""):
        word = raw.strip(_EDGE)
        if word and not re.fullmatch(r"[-–—]+", word):
            out.append(word)
    return out


LIMITS = (300, 2000, 3000)  # structured_resume field lengths (Short, bullets, descriptions)
_FOLD = str.maketrans({"‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-", "‘": "'", "’": "'", "“": '"',
                       "”": '"'})


def fold(word: str) -> str:
    """A word with dash and quote variants made plain: a parser writing "2-4" for "2–4" invents nothing."""
    return word.translate(_FOLD).casefold()


def norm(text: str) -> str:
    return fold(" ".join((text or "").split()).strip(" ."))


def edit_distance(a: str, b: str) -> int:
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def compare_tokens(expected: list[str], found: list[str]) -> dict[str, Any]:
    """Character errors between two bags of words, order ignored: exact matches first, then each remaining expected
    word is paired with the most similar remaining found word (a misreading); what stays unpaired is missing or extra."""
    left, right = Counter(expected) - Counter(found), Counter(found) - Counter(expected)
    missing, extra = list(left.elements()), list(right.elements())
    errors, pairs = 0, []
    for word in sorted(missing, key=len, reverse=True):
        best = max(extra, key=lambda w: SequenceMatcher(None, word, w).ratio(), default=None)
        if best is not None and SequenceMatcher(None, word, best).ratio() >= 0.5:
            extra.remove(best)
            errors += edit_distance(word, best)
            pairs.append(f"{word}→{best}")
        else:
            errors += len(word)
            pairs.append(f"{word}→∅")
    errors += sum(len(w) for w in extra)
    return {"chars": sum(len(w) for w in expected), "errors": errors, "wrong": pairs, "extra": extra}


def altered(expected: list[str], layer: list[str], final: list[str]) -> list[str]:
    """Expected words the text layer had right and the final text lost: a repair broke a valid word."""
    want = Counter(expected)
    had, kept = want & Counter(layer), want & Counter(final)
    return sorted((had - kept).elements())


def corrections(expected: list[str], layer: list[str], final: list[str]) -> tuple[int, int, list[str]]:
    """The changes from text layer to final text (aligned word sequences). Returns (decided, right, wrong examples).

    right    it produces the expected words in place of broken ones
    wrong    it changes words the layer had right, or produces a real word that is not the expected one
             ("Kaga" → "Saga"): a confident wrong correction
    neither  a partial repair that leaves the word still broken ("cerYﬁcaYon" → "cerYficaYon"): not counted
    """
    from app.services.text_repair import known

    want = set(expected)
    made = right = 0
    wrong = []
    for op, i1, i2, j1, j2 in SequenceMatcher(None, layer, final, autojunk=False).get_opcodes():
        before, after = layer[i1:i2], final[j1:j2]
        if op == "equal" or "".join(before) == "".join(after):
            continue  # unchanged, or only white space moved
        produces = all(w in want for w in after)
        destroys = bool(before) and all(w in want for w in before)
        misleads = any(w not in want and not w.endswith("-") and known(w) for w in after)  # "production-" is half a word
        if produces and not destroys:
            made, right = made + 1, right + 1
        elif destroys or misleads:
            made += 1
            wrong.append(f"{' '.join(before)}→{' '.join(after)}")
    return made, right, wrong


# ───────────── structure metrics ─────────────

def _texts(items: list[Any], *keys: str) -> list[str]:
    out = []
    for item in items or []:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            out += [str(item.get(k) or "") for k in keys if item.get(k)]
    return out


def produced_facts(data: dict[str, Any]) -> dict[str, list[Any]]:
    roles = data.get("experience") or []
    return {
        "skills": [s.get("name", "") for s in data.get("skills") or []],
        "roles": [(r.get("job_title", ""), r.get("company", "")) for r in roles],
        "dates": [(r.get("company", ""), r.get("start_date"), r.get("end_date")) for r in roles],
        "bullets": [b for r in roles for b in _texts(r.get("responsibilities"), "text")
                    + _texts(r.get("achievements"), "text")],
        "education": [(e.get("degree", ""), e.get("institution", "")) for e in data.get("education") or []],
        "certifications": [c.get("name", "") for c in data.get("certifications") or []],
        "projects": [p.get("name", "") for p in data.get("projects") or []],
        "summary": [data["summary"]] if isinstance(data.get("summary"), str) and data["summary"].strip() else
                   [data["summary"].get("text", "")] if isinstance(data.get("summary"), dict) else [],
    }


def expected_facts(resume: dict[str, Any]) -> dict[str, list[Any]]:
    return {
        "skills": list({norm(i["name"]): i["name"] for g in resume["skills"] for i in g["items"]}.values()),  # once each
        "roles": [(r["title"], r["company"]) for r in resume["experience"]],
        "dates": [(r["company"], r["start"], r["end"]) for r in resume["experience"]],
        "bullets": [b for r in resume["experience"] for b in r["bullets"]],
        "education": [(e["degree"], e["institution"]) for e in resume.get("education", [])],
        "certifications": [c["name"] for c in resume.get("certifications", [])],
        "projects": [p["name"] for p in resume.get("projects", [])],
        "summary": [resume["summary"]],
    }


def _key(value: Any) -> Any:
    return tuple(norm(v) if isinstance(v, str) else v for v in value) if isinstance(value, tuple) else norm(value)


def _flat(value: Any) -> list[str]:
    return [v for v in value if isinstance(v, str) and v] if isinstance(value, tuple) else [value] if value else []


def score_structure(resume: dict[str, Any], data: dict[str, Any], final_text: str) -> dict[str, Any]:
    want, got = expected_facts(resume), produced_facts(data)
    correct = produced = expected = 0
    missing, misfiled, hallucinated = [], [], []
    seen_text = norm(final_text)
    # A value is invented when it holds a word that neither the resume nor the extracted text has: a misread word
    # is the extraction's error, a value merged from two sections is misfiled, neither is invented.
    vocabulary = {fold(w) for w in tokens(" ".join(document_lines(resume))) + tokens(final_text)}

    def invented(part: str) -> bool:
        words = [fold(w) for w in tokens(part)]
        for i, w in enumerate(words):
            if w in vocabulary:
                continue
            cut = len(part) >= min(LIMITS)  # a value as long as a field's limit was cut there (structured_resume)
            if i == len(words) - 1 and cut and any(v.startswith(w) for v in vocabulary):
                continue  # its last word may be partial ("s" for "systems"): cut, not invented
            return True
        return False
    for field, values in want.items():
        found = Counter(_key(v) for v in got[field])
        expected += len(values)
        produced += len(got[field])
        for value in values:
            if found[_key(value)]:
                found[_key(value)] -= 1
                correct += 1
                continue
            missing.append(f"{field}: {value}")
            if field == "dates":
                continue  # a role's dates go missing with the role: counted once, under roles
            elsewhere = " ".join(norm(str(v)) for other, vs in got.items() if other != field for v in vs)
            if any(norm(part) in elsewhere for part in _flat(value) if len(norm(part)) > 3):
                misfiled.append(f"{field}: {value}")
    for field, values in got.items():
        if field == "dates":
            continue  # normalised (2020-08): the raw dates are words of the text
        for value in values:
            for part in _flat(value):
                if invented(part):
                    hallucinated.append(f"{field}: {part}")
    everything = norm(json.dumps(data, ensure_ascii=False))
    dropped = [i["name"] for g in resume["skills"] for i in g["items"]
               if i.get("recognized") is False and norm(i["name"]) not in everything]
    bullets = [b for r in resume["experience"] for b in r["bullets"]]
    intact = sum(norm(b) in seen_text for b in bullets)
    return {"facts_expected": expected, "facts_produced": produced, "facts_correct": correct, "missing": missing,
            "misfiled": misfiled, "hallucinated": hallucinated, "unrecognized_dropped": dropped,
            "bullets": len(bullets), "bullets_intact": intact}


# ───────────── the suite ─────────────

def _real_examples() -> list[tuple[str, Path, str, dict[str, Any]]]:
    kinds = {".pdf": "application/pdf", ".txt": "text/plain",
             ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
    out = []
    for truth in sorted(REAL.glob("*.json")):
        doc = next((truth.with_suffix(s) for s in kinds if truth.with_suffix(s).exists()), None)
        if doc is not None:
            out.append((f"real-{truth.stem}", doc, kinds[doc.suffix], json.loads(truth.read_text())))
    return out


def run(ds: Dataset, ctx: dict[str, Any]) -> SuiteResult:
    name = ctx.get("resume_pipeline", "current")
    pipeline = PIPELINES[name]
    resumes = load_resumes()
    docs = [(ex.key, DOCUMENTS / ex.inputs["document"], ex.inputs["kind"], resumes[ex.outputs["resume"]],
             ex.metadata.get("variant", "clean")) for ex in ds.examples if "document" in ex.inputs]
    if ctx.get("real_resumes"):
        docs += [(key, path, kind, truth, "real") for key, path, kind, truth in _real_examples()]
    cases: list[dict[str, Any]] = []
    totals: Counter[str] = Counter()
    by_variant: dict[str, Counter[str]] = {}
    altered_words: list[str] = []
    wrong_corrections: list[str] = []

    def add_text(variant: str, expected: list[str], layer: list[str], final: list[str]) -> dict[str, Any]:
        cmp = compare_tokens(expected, final)
        lost = altered(expected, layer, final)
        made, right, wrong = corrections(expected, layer, final)
        for bucket in (totals, by_variant.setdefault(variant, Counter())):
            bucket.update({"chars": cmp["chars"], "errors": cmp["errors"]})
        totals.update({"corrections": made, "corrections_right": right})
        altered_words.extend(lost)
        wrong_corrections.extend(wrong)
        return {"cer": _rate(cmp["errors"], cmp["chars"]), "wrong_words": cmp["wrong"][:8], "altered": lost,
                "bad_corrections": wrong[:5]}

    for key, path, kind, resume, variant in docs:
        data = path.read_bytes()
        layer, final, context = pipeline.read(kind, data)
        expected = tokens("\n".join(document_lines(resume)))
        out = add_text(variant, expected, tokens(layer), tokens(final))
        s = score_structure(resume, pipeline.structure(final, context), final)
        totals.update({k: s[k] if isinstance(s[k], int) else len(s[k]) for k in
                       ("facts_expected", "facts_produced", "facts_correct", "misfiled", "hallucinated",
                        "unrecognized_dropped", "bullets", "bullets_intact")})
        out.update({"recall": _rate(s["facts_correct"], s["facts_expected"]), "missing": s["missing"][:6],
                    "misfiled": s["misfiled"][:6], "hallucinated": s["hallucinated"][:6],
                    "unrecognized_dropped": s["unrecognized_dropped"]})
        if pipeline.route is not None and kind == "application/pdf" and variant in EXPECTED_ROUTE:
            out["route"] = pipeline.route(kind, data)
            totals.update({"routed": 1, "routed_right": out["route"] in EXPECTED_ROUTE[variant]})
        ok = (not out["wrong_words"] and not out["altered"] and s["facts_correct"] == s["facts_expected"]
              and s["facts_produced"] == s["facts_expected"] and not s["hallucinated"])
        cases.append({"key": key, "ok": ok, "outputs": out})
    for ex in ds.examples:
        if "text_layer" not in ex.inputs:
            continue
        layer = ex.inputs["text_layer"]
        out = add_text("text", tokens(ex.outputs["text"]), tokens(layer), tokens(pipeline.repair(layer)))
        cases.append({"key": ex.key, "ok": not out["wrong_words"] and not out["altered"], "outputs": out})

    metrics: dict[str, Any] = {
        "pipeline": name,
        "cases": len(cases),
        "cer": _rate(totals["errors"], totals["chars"]),
        **{f"cer_{variant}": _rate(c["errors"], c["chars"]) for variant, c in sorted(by_variant.items())},
        "valid_words_altered": len(altered_words),
        "corrections": totals["corrections"],
        "correction_precision": _rate(totals["corrections_right"], totals["corrections"]),
        "field_precision": _rate(totals["facts_correct"], totals["facts_produced"]),
        "field_recall": _rate(totals["facts_correct"], totals["facts_expected"]),
        "misfiled": totals["misfiled"],
        "hallucinated_values": totals["hallucinated"],
        "unrecognized_dropped": totals["unrecognized_dropped"],
        "bullets_intact": _rate(totals["bullets_intact"], totals["bullets"]),
        "quality_routing": _rate(totals["routed_right"], totals["routed"]),  # None: the pipeline routes nothing
    }
    return SuiteResult("resume_extraction", ds.identity(), metrics, cases)
