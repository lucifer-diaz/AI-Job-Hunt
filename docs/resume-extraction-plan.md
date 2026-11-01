# Resume extraction V2: Phase 0 findings and plan

Status: **Phases 1 – 6 done; the resume_extraction gate passes for V2. Phase 7 (per-fact confidence) is conditional.** Agreed decisions are in section 10; where they differ from the
recommendations in sections 5 – 9, section 10 wins.

## 1. Summary

* **The ligature hypothesis holds, and the fix is cheaper than the spec assumes.** The real resume PDF in this
  install (Calibri, exported from Apple Pages through Quartz) draws `ti`, `fk`, `ft`, `tt`, `tf` … as single
  ligature glyphs whose `ToUnicode` entries are wrong (`Y`, `Z`, `[`, `B`, `4`, `C`, and lower-case `g` for `fk`).
  **But the same PDF also carries the correct text for every ligature in `/ActualText` marked content** (140 spans:
  `ti`×95, `fl`×11, `tf`×7, `fk`×5 …). pypdf, which we use today, ignores `/ActualText`; PDFium and MuPDF honour it.
* Switching the text layer from pypdf to **pypdfium2** (Apache-2.0/BSD, PDFium) removes **every** word error in that
  file: 0 word differences against the reference, versus 46 residual errors with today's pypdf + `text_repair`
  (`Kaga`×4, `So4ware`×4, `producYonize`, `Yme`, `hCps://` …). It is also ~25× faster and renders pages for OCR, so
  it replaces two dependencies at once.
* Much of the target architecture already exists here under other names: grounding in code
  (`resume_parsing.ground`), per-unit verification (`UNVERIFIED/VERIFIED/REJECTED/EDITED`), a compare UI, a
  deterministic glyph repair (`text_repair`), and tailoring/review restricted to VERIFIED facts. The plan below
  **extends those** instead of building a parallel `StructuredResume`.
* Four points in the spec conflict with rules this codebase already enforces (section 6). They need your decision
  before Phase 1.

## 2. Current architecture

Python 3.11/3.12, FastAPI, SQLAlchemy + Alembic (PostgreSQL in production, SQLite in tests), vanilla JS frontend
(`app/static/*.js`), LangGraph for worker graphs.

```
POST /api/profile/resume                       app/main.py:966  resume_upload
  → document_text.extract(kind, bytes)         app/services/document_text.py
      PDF : pypdf page.extract_text() → text_repair.repair_glyphs
      DOCX: python-docx paragraphs only (tables, text boxes, headers are not read)
      TXT : utf-8
  → structured_resume.clean_raw_text           ligature chars ﬁ ﬂ … → letters, control chars removed
  → ResumeDocument (file on disk, extracted_text) + resume_verification.record_version → Resume
      Resume.raw_text      read-only reference; ranking identity hashes it
      Resume.structured_data (JSON)  ← structured_resume.parse_deterministic (regex, instant)
  → resume_parsing.enqueue → ResumeParseTask   (no model in the request)

scheduler job `resume_parse` → resume_parsing_graph.run (LangGraph, or plain `process`)
  LOAD_RESUME → EXTRACT_STRUCTURE → GROUND_IN_TEXT → CHECK_QUALITY → APPLY_RESULT
     model: worker RESUME_PARSER via LLMClient.generate_prompt, prompt app/prompts/resume_parser.py (v6),
            contract app/prompts/contracts.py::ResumeParserOutput
     ground: resume_parsing.ground — a value is kept only if the raw text contains it; otherwise DROPPED
     apply : fills structured_data only while `untouched`, else a value-by-value comparison
```

The same `document_text.extract` reads uploaded job descriptions (`app/workspace.py:1275`).

**Data model.** Facts are not columns: they are units inside `Resume.structured_data` (JSON), each with a stable
`id` and `verification: {status, updated_at}` (`structured_resume.materialize`, `iter_units`). Changes are logged
in `ResumeDataChange` (source PARSER / USER / SYSTEM). VERIFIED bullets under a VERIFIED role become `Evidence` rows.

**Review UI.** `app/static/resume_data.js`: per-unit Verify / Reject / Unverify, per-section verify, Edit, and a
"Keep current / Use new" comparison after a re-parse. The AI parse status line shows how many values grounding dropped.

**Downstream readers.** Everything resolves the resume through `resume_verification.for_job / selected`.

| Reader | What it uses today |
| --- | --- |
| Tailoring (`context_builder.build_tailoring_context`, MCP `get_tailoring_context`) | VERIFIED units only (`VERIFIED_ONLY`); refuses with `UnverifiedResumeError` when none, unless the policy `allow_unverified_resume_facts` is on |
| Review (`build_review_context`, MCP review tools) | VERIFIED only, same policy switch |
| Ranking | `master_resume` = all non-REJECTED units rendered (`render_document`), plus unverified facts when `ranking_uses_unverified` (default on) |
| `save_tailored_resume` | validated against verified evidence (`tailoring_results`) |
| `begin_application_submission` / apply queue | the approved packet (digest-checked) and profile contact fields; no resume units directly |

So **gating [L] already exists for tailoring and review** and is stricter than the spec's rule (see 6.1).

## 3. Root cause: evidence

Two uploaded PDFs exist in this install (`data/resumes`, gitignored). Only tokens and font metadata were inspected;
no resume text was copied anywhere.

| | Calibri resume (Pages → Quartz) | Inter resume (Pages → Quartz) |
| --- | --- | --- |
| Fonts | Calibri, -Bold, -Italic, -BoldItalic; TrueType subsets, WinAnsi, ToUnicode present | Inter (MacRoman, no ToUnicode) + Times fi/fl |
| Glyph names | stripped (`glyph00001` …): glyph-name repair is impossible | real names (`fi`, `fl`) in Times only |
| `/ActualText` spans | 140: ti 95, fl 11, tf 7, fi 5, **fk 5**, ft 4, ff 1, ffi 1, tt 1 | — |
| pypdf (current) | `Kaga`, `opYmizing`, `pla[orm`, `PlaBorm`, `QualiﬁcaZon`, `So4ware`, `hCps`, `ﬂeet` … | 23 Private Use Area chars (U+E081 …) |
| pypdf + `text_repair` + `clean_raw_text` (today's output) | **46 token errors** remain: Kaga ×4, So4ware ×4, producYonize ×2, Yme/Ymes, sensiYve, noYficaYons, telemaYcs, qualificaYon, hCps | PUA chars remain |
| pdftotext (poppler) | ligature glyphs dropped entirely (`produc`, `integra`) | clean |
| pdfminer.six | same errors as pypdf (no ActualText) | PUA chars |
| **pypdfium2** | **0 word errors**; 2 line-break hyphens come back as U+FFFE (map to `-`) | clean |
| PyMuPDF | 0 word errors (reference) | clean |

Why today's repair misses them: `text_repair` only maps an *odd* character between lower-case letters. It cannot
see `g` (a real letter: `Kaga`), digits (`4` is "ordinary": `So4ware`), a glyph at the start of a word (`Yme`), or
an upper-case letter inside a URL (`hCps`). That is by design ("a wrong correction is worse than a missed one"), and
it is exactly the gap the spec describes. "Transformayon" (lower-case `y`) does not occur in these two files; it
belongs to the same class as `Kaga` and would be recovered the same way where `/ActualText` exists.

Implication: at least the producer that caused this (Pages/Quartz) ships the right text alongside the broken
mapping. Whether Word, Google Docs and Canva exports do is unverified; the Phase 1 eval measures it on exports from
each. PDFs without ActualText still need [B] OCR + [E] correction, so those stages stay in the plan, but as the
second line, not the first.

## 4. Environment

| Package | State | Licence | Proposal |
| --- | --- | --- | --- |
| pypdf 6.16 | installed | BSD | keep as fallback reader |
| **pypdfium2** | not installed, ~3 MB wheel, no system deps | Apache-2.0 / BSD-3 | **add** (text layer with char boxes + fonts, 300 DPI rendering) |
| PyMuPDF | not installed | **AGPL-3.0** | **do not add**: this app is deployed as a network service |
| pdfminer.six | not installed | MIT | not needed (no ActualText) |
| fontTools | not installed | MIT | not needed now (glyph names are stripped in the real file) |
| **rapidfuzz** | not installed | MIT | **add** (fuzzy grounding fallback, lexicon match) |
| Tesseract 5.5 | Homebrew binary present locally (eng); **not in the Docker image** | Apache-2.0 | **add** `tesseract-ocr` to the Dockerfile (apt, ~30 MB) behind a build arg like Chromium; call through a small subprocess wrapper (or pytesseract) |
| Pillow 12.3 | installed | HPND | used for crops |
| PaddleOCR / docTR / Docling / Marker | not installed, heavy (torch/onnx) | various | not proposed; revisit only if the eval shows Tesseract is the bottleneck |
| WeasyPrint | not installed | BSD | not needed: Playwright Chromium (already a dependency) prints synthetic HTML to PDF |
| pikepdf | not installed | MPL-2.0 | dev/test only, to build broken-ToUnicode fixtures (or pypdf's writer, already installed) |

Tests run with `conda activate jobhunt`. There is no CI workflow in the repo; the eval runs as a pytest gate plus a
suite in the existing evaluation framework (`app/services/agent_eval`, `scripts/agent_eval.py`, `gates.GATES`).

## 5. How the target architecture maps onto this codebase

New package `app/services/resume_extraction/` (deterministic, no model, no network), called from
`document_text.extract` and the `resume_parse` task. Every stage is a pure function over data classes and is unit
tested on its own.

| Stage | Module | Adaptation |
| --- | --- | --- |
| [A] text layer | `text_layer.py` | pypdfium2 `PdfTextPage`: chars with boxes and font names, ActualText honoured. NFKC, U+FFFE / soft hyphen → `-`, line-break hyphenation joined only when the joined word is known. Flags `(cid:NN)`, U+FFFD, PUA, control chars. pypdf stays as a fallback when PDFium cannot open a file. |
| [A] glyph repair | `glyph_repair.py` | today's `text_repair` moves here unchanged and keeps running on text-layer output (it is byte-identical on clean text). Glyph-name decoding is **dropped from scope**: the real fonts have stripped `post` tables, and ActualText supersedes it. |
| [B] OCR | `ocr.py` | `OcrBackend` protocol; `TesseractBackend` (local subprocess, TSV output → words + boxes + conf); `NullBackend` when the binary is missing (Monitoring says so, like Playwright). PDFium renders at 300 DPI. No external OCR/VLM; a VLM backend would be a config switch defaulting off. |
| [C] align | `align.py` | OCR pixel boxes → PDF points; IoU match, one-to-many merge. |
| [D] quality | `quality.py` | OOV rate (the bundled English list + `MODERN` + skill/company lexicons), bad-char count, OCR disagreement, empty text layer → OCR-primary. Routes: clean → no OCR; suspicious tokens → crop OCR; poor → full OCR. Thresholds in `server_settings`. |
| [E] correction | `correction.py` + `lexicons/confusions.yaml` | the spec's rules as written: per-document rules accepted on ≥2 distinct OOV tokens, applied only to OOV tokens that become known, OCR agreement, confusion-weighted lexicon match needing a second signal. Every change is a `Correction` record. |
| [F] layout | `layout.py` | column detection by x-gap on PDFium line boxes; section headings by size/weight + the keyword list already in `structured_resume.HEADING_RE`. Docling/Marker only if the eval says the heuristic fails (needs your approval: heavy). |
| [G] LLM | existing `resume_parser` prompt (v7) + `ResumeParserOutput` contract | through `LLMClient.generate_prompt` and the RESUME_PARSER worker, as today. Input: section-segmented clean text + the correction list, wrapped in `untrusted()`. |
| [H] validate | `validate.py`, called by `resume_parsing.ground` | extends today's `Source` checks: quote/raw term located in clean, raw or OCR text; numbers exact; LLM-proposed corrections checked against [E]'s evidence; rapidfuzz `partial_ratio` as the last fallback. |
| [I] confidence | `confidence.py` | the spec's table, thresholds in config, reasons as strings. |
| [J] review | `app/static/resume_data.js` | a "Corrections" panel (`Kaga → Kafka` with accept / reject / edit), confidence badges with reason tooltips, low/ungrounded items listed for resolution, optional page crop. |
| [K] persistence | see 6.3 | per-unit `extraction` object in the JSON + two owned tables. |
| [L] gating | `context_builder` | already VERIFIED-only; see 6.1. |

**Where each stage runs.** The upload request keeps doing only what is cheap: the PDFium text layer, NFKC, glyph
repair and the quality score (milliseconds). OCR, alignment and correction run in the `resume_parse` task, as a
new first step `REPAIR_TEXT` before `LOAD_RESUME`, registered in the graph, `STEPS`, Running workers and the
orchestration topology (AGENTS.md "Any new AI workflow stage"). Nothing heavy ever runs in a request.

**Feature flag.** `RESUME_EXTRACTION_V2` (server setting, off). Off = `document_text.extract` and the parser exactly
as today, byte for byte (a test pins it). Proposed exception: the pypdfium2 text layer alone (section 7, Phase 2a)
could ship on by default because it corrects text without inventing any; your call.

**Evaluation.** `tests/fixtures/resume_extraction/` (versioned dataset, ground-truth JSON) and an
`agent_eval` suite `resume_extraction` with the spec's metrics as gates (hallucination and valid-words-altered at 0
are hard gates). Synthetic fixtures: HTML → PDF through Playwright Chromium with ligature-heavy fonts in
single/two-column/sidebar/table layouts; **Chromium writes correct ToUnicode maps**, so a fixture builder then
rewrites the ligature entries to wrong single characters and strips `/ActualText` to reproduce the bug faithfully
(both with and without ActualText). Plus text-level corruption cases from the confusion table, and a documented
`real/` slot (gitignored by default) for anonymised resumes you add later.

## 6. Conflicts with existing rules: decisions needed

### 6.1 Gating rule [L]
Spec: tailoring/submission may use facts that are `user_verified` **or** `confidence = high`. Today tailoring and
review use **VERIFIED only** (unless the person turns on `allow_unverified_resume_facts`). The spec's rule would
loosen that, and AGENTS.md says "Only the user verifies."
**Recommendation:** keep VERIFIED-only. Use confidence to make review fast instead: high-confidence units are
pre-selected in a one-click "Accept all high-confidence items" that the person presses (it writes VERIFIED through
`resume_verification`), and low/ungrounded items block that bulk action until resolved.

### 6.2 Ungrounded values
Spec: keep ungrounded values, flagged low. AGENTS.md: "Grounding drops, never rewrites … never loosen it."
Today a value the LLM normalised away from a corrupted source (`Kafka` when the text says `Kaga`) is **silently
dropped**, which is one of the bugs here.
**Recommendation:** `structured_data` keeps holding grounded values only. Ungrounded and unsupported-correction
items go to a per-resume review list (the extraction record) where the person can add, edit or discard them; they
never reach a workflow unless the person adds them. Nothing is silently lost, and the invariant holds.

### 6.3 Per-fact DB columns [K]
Facts are units inside `Resume.structured_data` JSON, not rows, so "add columns per fact" does not apply.
**Recommendation:** each unit gains an optional `extraction` object (`raw_term`, `was_corrected`,
`correction_id`, `grounded`, `confidence`, `confidence_reasons`, `source_page`); `user_verified` is the existing
`verification.status == VERIFIED`. A missing object means unknown (the spec's backfill), so no data migration.
One Alembic migration adds two owned tables (tenancy-registered, cascade on account delete):
`resume_extractions` (per Resume: quality report, corrections, review list, OCR used, pipeline version) and
`resume_extraction_feedback` (accept / reject / edit decisions). The public `ResumeParserOutput` contract changes
(new fields) — this is the "public profile schema" change you asked to approve.

### 6.4 `source_quote` on every leaf [G]
A verbatim quote for every skill, date and field roughly doubles the parser's output tokens (it already runs near
answer caps on long resumes; see `model_capacity`). Bullets are their own quote.
**Recommendation:** the model returns `raw` (exactly as written) and `value` (normalised) per leaf, plus an
optional correction; **code** locates `raw` in the text and records page and offsets. That is stricter (the
position is found, not claimed) and cheaper. Keep a `source_quote` only for summary sentences, where a span helps.

## 7. Proposed phases (adapted)

1. **Eval harness + baseline** on the current pipeline (fixtures, fixture builder, suite, gates).
2. **[A] + [D]**. 2a: pypdfium2 text layer + U+FFFE/hyphen handling, glyph repair kept as is. 2b: quality report.
3. **[B] + [C] + [E]**: Tesseract backend, alignment, correction engine (for PDFs without ActualText).
4. **[F]**: columns and sections; decide on Docling only with eval numbers in hand.
5. **[G] + [H] + [I]**: parser prompt v7, contract, validator, confidence.
6. **[K]**: migration, extraction record, flag `RESUME_EXTRACTION_V2`.
7. **[J]**: review UI (corrections panel, badges, review list, crops); DOCX table text.
8. **[L]**: per 6.1. Application submission logic is not touched (it reads the approved packet).

## 8. Other risks and questions

* **Version identity.** `Resume.raw_text` is hashed for ranking identity and versions. With V2 on, a re-upload of
  an existing PDF yields corrected text, so a new hash and a new UNVERIFIED version (re-ranking cost). Existing
  versions are never re-extracted automatically; a "Re-read file" action could be offered later. OK?
* **Where corrected text lives.** Proposal: `Resume.raw_text` = text-layer text after deterministic repair
  (ActualText, NFKC, glyph repair: what the file says); OCR/lexicon corrections are applied to a `clean_text` kept
  on the extraction record, and the parser reads `clean_text`. Grounding accepts either.
* **Job descriptions** go through the same `document_text.extract` and would get the PDFium reader too. Fine, or
  keep JD uploads on pypdf?
* **Tesseract in production** adds ~30 MB to the image; without it, V2 degrades to text layer + rules and says so.
* **Your two resume PDFs** are the only real fixtures. Using them (or derived token statistics) in the committed
  eval needs your explicit consent; by default they are only used locally and never committed.
* **DOCX** is supported but reads paragraphs only; resumes built with tables or text boxes lose content. Adding
  table cells is cheap and planned for Phase 7.
* **No CI** in the repo: gates run through pytest and `scripts/agent_eval.py` locally. Want a GitHub Actions
  workflow added?
* Logs carry counts, rule names (`Y→ti ×12`) and ids only, never resume text; correction records (which contain
  resume words) live only in the owned tables.

## 9. Approvals requested

1. Dependencies: pypdfium2, rapidfuzz, Tesseract in the Docker image (pikepdf for tests only). No PyMuPDF (AGPL).
2. Decisions 6.1 – 6.4.
3. Whether Phase 2a (PDFium reader) ships on by default or behind `RESUME_EXTRACTION_V2`.
4. Consent question on the local PDFs (section 8).

## 10. Agreed decisions (2026-11-01)

* **Everything new is behind `RESUME_EXTRACTION_V2`**, the PDFium reader included. Off = today's extraction and
  parsing, byte for byte.
* **Gating stays VERIFIED-only** (6.1). Confidence speeds review up; it never grants access.
* **Ungrounded values go to a review list** (6.2); `structured_data` keeps holding grounded values only.
* **Per-unit `extraction` object + two owned tables** (6.3); **code locates values** instead of model-written quotes
  (6.4).
* **Confidence is built where it decides something**: first on corrections, fuzzy grounding and section mappings
  (shown in the corrections and sections panels); per-fact badges and "accept all high-confidence" only if the eval
  shows enough corrected or uncertain values to justify them.
* **Section mapping is reviewable.** Today an unrecognised heading ("Core Skills", "Areas of Expertise", "Career
  History", "Tech Stack") is not a heading at all: its lines run on into the previous section (measured: skills and a
  whole role filed under the summary). V2 detects headings from layout, maps them through a synonym table with a
  recorded method (synonym / guess / unknown), never merges an unknown section into its neighbour, lets the person
  assign each heading in a Sections review step, and learns the person's mappings per account (counting, no model).
* **One phase at a time, in one session.** Real local PDFs are used locally only, never committed.

### Phases (replaces section 7)

1. **Eval harness + baseline.** Synthetic fixtures built from hand-written resumes: an OFL font with real ligature
   glyphs, Chromium print-to-PDF in four layouts, then the ToUnicode entries of ligature glyphs rewritten to wrong
   characters, with and without `/ActualText`; heading variants; DOCX with tables; text-level corruption cases. Suite
   `resume_extraction` in `app/services/agent_eval` with the spec's gates. Baseline on today's pipeline.
2. **Text layer + quality.** PDFium reader, hyphenation, bad-character flags, quality report; glyph repair kept.
3. **Sections + persistence.** Layout heading detection, synonym table, no bleed, DOCX headings and tables; migration
   for the extraction record and feedback tables.
4. **Sections review.** Panel, remap → re-parse, learned mappings.
5. **Parser v7 + grounding.** Section map in, raw/value out, code locates values, ungrounded → review list.
6. **OCR + corrections** for PDFs without ActualText; corrections panel with confidence. Depth set by the Phase 1
   numbers and the real exports.
7. **Per-fact confidence** (conditional on the eval).

## 11. Progress

### Phase 1: eval harness + baseline (done)

* Ground truth: `tests/fixtures/resume_extraction/resumes.json` (four hand-written synthetic resumes, each with its
  own heading vocabulary and layout). Documents: `scripts/build_resume_fixtures.py` (Inter-derived OFL font with real
  ligature glyphs → Chromium PDF → clean / broken ToUnicode / broken + ActualText; DOCX incl. a skills table; TXT).
  The broken PDFs reproduce the real file exactly: pypdf reads `Kaga`, `So4ware`, `hCps://`.
* Suite `resume_extraction` (`app/services/agent_eval/resume_extraction.py`) with the spec's gates, a pluggable
  pipeline (`--resume-pipeline`, `current` today) and a local-only `real/` slot (`--with-real-resumes`).
* Baseline: `docs/evals/resume-extraction-baseline.md`. BLOCKED: CER 0.0186, field recall 0.36, 122 misfiled facts,
  4 unrecognised terms dropped; hard gates `valid_words_altered` and `hallucinated_values` at 0. Beyond the ligature
  bug it found that content before the first recognised heading is discarded (a whole sidebar), that bullets without
  a "•" character are lost (every DOCX upload on the regex path), and that DOCX tables are not read.
* `scripts/agent_eval.py` with no `--suite` now reports BLOCKED overall until V2 meets these targets.

### Phase 2: text layer + quality (done)

* `app/services/resume_extraction/`: `text_layer` (PDFium words with page, box, font, bold and glyph height; lines
  rebuilt from positions; PDFium's line-break hyphen marker kept as a soft hyphen; pypdf fallback), `normalize`
  (artefact characters only, never NFKC; line-break words joined on evidence), `quality` (route from unknown lower-case
  words, broken-glyph suspects and unmapped characters; product names such as "BigQuery" are not suspects), `pipeline`.
  `document_text.extract` uses it for PDFs when `RESUME_EXTRACTION_V2` is on.
* Glyph-name repair (fontTools) dropped as planned: ActualText supersedes it, and the real file's glyph names are stripped.
* Eval (`docs/evals/resume-extraction-phase2.md`): `cer_actualtext` 0.0194 → 0.0, CER 0.0191 → 0.0113 (dataset v2), bullets intact
  0.83 → 0.93, field precision 0.70 → 0.77, recall 0.36 → 0.40, quality routing 1.0; hard gates stay at 0. Both real
  PDFs in this install, and the two in `data/real/`, read with no broken word and route `clean`.
* A real export maps `ti` to the digit 5 and `tf` to G: the confusion table is per export, not per font, and the
  correction engine (Phase 6) must handle digit glyphs (`text-digit-glyph`, dataset v2).
* Found on the way: pypdfium2 5 returns char boxes as (left, bottom, right, top); boxes are ink bounds, so letter gaps
  are no evidence of a space; a comma hangs below the baseline, so a line is a band, not the previous glyph's height.

### Resumes tab redesign and real resumes (between Phases 2 and 3)

* Settings → Resumes simplified (10,500px / 215 buttons → about 5,800px): compact library with a menu per resume,
  a review header with progress and one next step (Review next → Verify resume), Review / Text / History tabs, sticky
  section chips, skills as chips, empty optional sections behind "+ Add section", one folded options panel.
* The new reader is an account worker setting (`resume_parsing.extraction_v2`) defaulting to the server's flag.
* The eval scores real resumes in the gitignored `real/` folder (`--with-real-resumes`).

### Phase 3: sections, structure, Word documents, extraction record (done)

* `sections.py` (synonyms, keywords, house style learnt from known headings; header protected), `structure.py`
  (line features from the text layer or Word paragraphs, columns from page gaps, the parse view: roles from real
  layouts, bullets without characters, wrapped lines joined), `docx_reader.py` (tables, list bullets, styles),
  `store.py`; migration 0065 (`resume_extractions`, `resume_extraction_feedback`, both owned).
* Upload with V2 records the extraction before the version is parsed; the regex parser (first parse, quick re-parse,
  the AI parse's quality check) reads the parse view; the resume detail carries `extraction` (route, sections).
* Eval (`docs/evals/resume-extraction-phase3.md`): field recall 0.396 → 0.930 (0.948 with the real resumes), precision
  0.769 → 0.936, misfiled 120 → 3, cer_clean 0.0; real resumes: all 7 roles right. Hard gates stay at 0.
* Fixed on the way: `delete_version` left a pending AI-parse task behind on SQLite (no cascade), which could block a
  later resume reusing the id.

### Phase 4: sections review (done)

* `store.assign` (a person's section for a heading, or "Not a section": feedback row, record overrides, view rebuilt
  from the stored text and its recorded look, re-parse filled while untouched else a comparison), `store.learned`
  (counted per heading as written, applied before the synonym table on the account's next uploads),
  `POST /api/resumes/{id}/sections`; migration 0066 (`layout`, `overrides`). An unplaced heading blocks
  `verify_resume`. The parser reads a view with `marked=True`, so a line a person said is not a heading stays a line.
* Settings → Resumes → Review: "How it was read" (quality route, each heading with its first line, Known / Guess /
  Choose / Yours / Your earlier choice, a section picker), open by itself when a heading needs a section.
* Eval unchanged by design (no person in it); certifications and awards wrapped in narrow columns are joined:
  precision 0.950 → 0.954 with the real resumes (synthetic 0.936 → 0.943).
* Not covered: in a text file an unknown heading is not detected at all, so it cannot be picked from the list (its
  lines stay where they are written); assigning arbitrary lines is a possible follow-up.

### Phase 5: AI parser on the parse view, raw terms, review list (done)

* Prompt v7 (`build_resume_parser_prompt(..., marked=True)`) for versions read with V2: sections and roles come
  marked, words are copied exactly (the v6 "fix obvious corruption" rule, whose fixes grounding always dropped, is
  gone), a normalized skill names its written form (`raw`). v6 is unchanged for every other version (golden tests).
* Grounding keeps a normalized skill when its `raw` is in the text and `normalizes(raw, name)` holds (containment,
  alias, initials; "Python" → "Rust" never); the unit keeps `raw` and the chip shows "written “Kafka”".
* `held_back` lists what grounding dropped (skills, bullets, roles, projects, certifications, awards, languages,
  education, summary, additional lines) on the record's review list, written under the task's lease; Settings →
  Resumes → "Held back for review" adds a skill, a bullet or a missing summary through the normal edit path (unverified)
  or discards; both are feedback rows. `POST /api/resumes/{id}/review`.
* Not built: per-field correction claims from the model (`was_corrected` + evidence) — Phase 6 owns corrections, with
  OCR and lexicon evidence; and an eval suite over recorded AI outputs (grounding is unit-tested; the resume_extraction
  suite scores the regex path).

### Phase 6: OCR and evidence-backed corrections (done)

* `ocr.py` (PDFium renders at 300 DPI; Tesseract through its own binary, TSV words in PDF points; pluggable; bounded
  by pages and time; never fails an upload), `align.py` (OCR words to text-layer words by box overlap),
  `correction.py` (only unknown or broken-looking words; the page's reading when it is a known word within two
  characters; per-file rules from `lexicons/confusions.yaml` and the page's disagreements, accepted on at least two
  different unknown words; each a Correction with evidence). Runs only for PDFs not routed `clean`.
* Decision taken here (supersedes §8 "where corrected text lives" only in naming): `Resume.raw_text` keeps the file's
  words; `store.clean_text` applies the corrections nobody undid and feeds the parse view and grounding; the record
  stores the corrections. Settings → Resumes lists them ("Kaga → Kafka", evidence, ×count) to keep or undo; undo
  re-reads the resume. OCR in the upload request, bounded, instead of a queue: a flagged two-page PDF adds ~2 – 5 s.
* Eval (`docs/evals/resume-extraction-phase6.md`, with Tesseract installed): the suite's gate **passes** for v2 —
  CER 0.0018 (broken PDFs 0.0194 → 0.0032), field precision / recall 0.983 / 0.983, misfiled 0, unrecognized dropped
  0, bullets intact 0.991, correction precision 1.0, no valid word altered, nothing invented. Without Tesseract
  broken PDFs fall back to rules and the gate depends on that environment.
* Left as written, by design: words only the page could confirm but no dictionary knows (names: "WhiBield",
  "SeaCle") and single unknown words with no page to read ("Kaga" in a text file).
