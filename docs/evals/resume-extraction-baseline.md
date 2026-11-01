# Resume extraction: Phase 1 baseline

Pipeline **`current`**, which is exactly what an upload does today: pypdf → `text_repair.repair_glyphs` →
`clean_raw_text` → `structured_resume.parse_deterministic`. The AI parser is not scored here (it needs a model); it
reads the same text, so every text error below reaches it too.

Dataset `resume_extraction/dataset` v1: 16 documents (4 synthetic resumes × PDF clean / broken / actualtext, plus
2 DOCX, 2 TXT) and 8 hand-written text-layer cases. Reproduce:

    python scripts/agent_eval.py --suite resume_extraction

Gate: **BLOCKED**, as expected before any V2 work.

| Metric | Baseline | Target | |
| --- | --- | --- | --- |
| `cer` (all) | 0.0186 | ≤ 0.005 | ❌ |
| `cer_clean` / `cer_broken` / `cer_actualtext` / `cer_text` | 0.0142 / 0.0194 / 0.0194 / 0.0683 | | |
| `valid_words_altered` | 0 | 0 (hard) | ✅ |
| `correction_precision` (149 corrections) | 1.0 | ≥ 0.99 | ✅ |
| `hallucinated_values` | 0 | 0 (hard) | ✅ |
| `unrecognized_dropped` | 4 | 0 (hard) | ❌ |
| `field_precision` | 0.696 | ≥ 0.98 | ❌ |
| `field_recall` | 0.362 | ≥ 0.95 | ❌ |
| `misfiled` | 122 | 0 | ❌ |
| `bullets_intact` | 0.830 | ≥ 0.98 | ❌ |

## What fails, by cause

1. **Ligature glyphs (text).** Broken and ActualText variants read identically, because pypdf ignores
   `/ActualText`: `Kafka → Kaga`, `Software → So4ware`, `lifted → li4ed`, `https → hCps`, `attention → aCenYon`,
   `Certifications → CerZficaZons`, `Whitfield → WhiBield`. The existing repair is safe (no valid word altered, every
   correction right) but recovers only odd symbols between lower-case letters; lower-case letters (`g`, `y`),
   digits (`4`), capitals at a word's start and URLs stay broken. Text cases `text-lowercase-g`,
   `text-lowercase-y` and `text-single-occurrence` are untouched.
2. **Line-break hyphens.** `produc-` / `tion`, `real-` / `time`, `click-` / `through` stay split
   (`text-line-break-hyphens`, CER 0.43 on that case).
3. **Unrecognised headings (structure, the largest loss).** "Core Skills", "Work History", "Areas of Expertise",
   "Career History", "Tech Stack", "Professional Experience", "Licenses & Certifications", "Academic Background" are
   not headings to the parser: their content runs on into the previous section (122 facts misfiled, mostly into the
   summary).
4. **Content before the first recognised heading is discarded.** In the sidebar layout the sidebar ("Areas of
   Expertise", "Academic Background") comes first, is treated as the contact header and is lost: every skill
   (`Tessellate DSL` among them) and the degree. Recall on Chloe's resume: 0.
5. **Bullets without a bullet character are lost.** Chromium draws list markers as shapes (single and sidebar
   layouts), and Word's bullets are list numbering, not text: with no "•" in the text the parser sees no bullets, and
   every experience bullet of `ana-single-*` and `ana-docx` is gone. This affects **every DOCX upload** on the regex
   path.
6. **DOCX tables are not read.** `dev-docx` keeps its skills in a table: all of them, `Brightlane SDK` included,
   are missing (CER 0.125).

## What Phase 2 is expected to move

PDFium (`/ActualText`) should take `cer_actualtext` to about `cer_clean`; hyphen joining moves `cer_text` and
`bullets_intact`. Causes 3 – 6 belong to Phase 3 (sections, DOCX), and `cer_broken` without ActualText to Phase 6
(OCR + corrections). The hard gates that pass today (`valid_words_altered`, `hallucinated_values`) must stay at 0
through every phase.

No real resume was scored: `tests/fixtures/resume_extraction/real/` is empty. Exports of one resume from Word,
Google Docs and Canva would show how many real producers write `/ActualText`.
