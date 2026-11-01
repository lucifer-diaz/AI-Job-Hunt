# Resume extraction: Phase 3 (sections, structure, Word documents, extraction record)

Dataset `resume_extraction/dataset` v2 (`5ff9c659e12a`), 25 synthetic cases; "+ real" adds the two real resumes in the
gitignored `real/` folder (`--with-real-resumes`, ground truth drafted from an independent extractor and the rendered
pages, local only).

    python scripts/agent_eval.py --suite resume_extraction [--resume-pipeline v2] [--with-real-resumes]

| Metric | current | v2 (Phase 2) | **v2 (Phase 3)** | v2 + real | Target |
| --- | --- | --- | --- | --- | --- |
| `field_recall` | 0.362 | 0.396 | **0.930** | 0.948 | ≥ 0.95 |
| `field_precision` | 0.696 | 0.769 | **0.936** | 0.950 | ≥ 0.98 |
| `misfiled` | 122 | 120 | **3** | 3 | 0 |
| `unrecognized_dropped` | 4 | 4 | **1** | 1 | 0 (hard) |
| `bullets_intact` | 0.830 | 0.929 | 0.929 | 0.948 | ≥ 0.98 |
| `cer` | 0.0191 | 0.0113 | **0.0050** | 0.0032 | ≤ 0.005 |
| `cer_clean` (incl. DOCX) | 0.0142 | 0.0130 | **0.0** | 0.0 | |
| `cer_actualtext` / `cer_real` | 0.0194 / 0.0113 | 0.0 | 0.0 | 0.0 / 0.0 | |
| `cer_broken` / `cer_text` | 0.0194 / 0.0758 | 0.0133 / 0.0465 | 0.0133 / 0.0465 | | Phase 6 |
| `valid_words_altered` / `hallucinated_values` | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 (hard) |
| `quality_routing` | — | 1.0 | 1.0 | 1.0 | |

Gate: BLOCKED (recall, precision, misfiled, bullets and the one dropped term), as planned until Phases 5 – 6.

## What moved, and why

* **Headings are found, not guessed** (`sections.detect`): a synonym table (Core Skills, Areas of Expertise, Tech
  Stack, Career History, Work History, Licenses & Certifications …), keywords, and the document's own heading style
  learnt from the headings the table knows, so a bold role title or project name is never a section. Every clean
  fixture and both real resumes get exactly their sections, each recorded with how it was placed.
* **Nothing bleeds or vanishes**: the sidebar resume (skills and education above the summary) went from recall 0 to
  0.95; an unknown heading in the house style becomes its own section.
* **Roles from real layouts** (`structure.view`): employer line + title line, several roles under one employer
  (Zendrive, Upwork), a title over its own dates; columns from the page's gaps. Both real resumes: all 7 roles with
  the right title, employer, location, dates and bullet count (recall 0.56 → 0.99).
* **Bullets without a character** (drawn markers, Word numbering) split where sentences end; wrapped headers, dates,
  skill lists and project descriptions in narrow columns are joined again.
* **Word documents**: tables (a skills grid) and list bullets are read (`docx_reader`); `dev-docx` recall 0.21 → 1.0.

## What is left

* `cer_broken`, `cer_text`, glyph-broken headings ("Areas of ExperZse"): Phase 6 (OCR + corrections).
* Education "Bachelor of Technology, Computer Science" and certification names with " – ": the shared regex parser
  splits on commas and dashes; the AI parser (Phase 5) reads field of study and full names.

Measurement fixes made on the way (none changes a synthetic baseline): role headers may be recorded as the document
writes them; dashes and quotes are folded before matching; a value cut at a field's length limit, or a word cut at a
line-break hyphen, is not counted as invented or as a wrong correction; a skill listed twice is one fact.
