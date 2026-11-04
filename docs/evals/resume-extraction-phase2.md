# Resume extraction: Phase 2 (text layer + quality)

Dataset `resume_extraction/dataset` v2 (`5ff9c659e12a`: the baseline's v1 plus `text-digit-glyph`, a ligature read as a digit — "Solu5ons", "PlaGorm" — as one real Pages export does), two pipelines:

    python scripts/agent_eval.py --suite resume_extraction                          # current
    python scripts/agent_eval.py --suite resume_extraction --resume-pipeline v2     # RESUME_EXTRACTION_V2

| Metric | current | v2 | Target |
| --- | --- | --- | --- |
| `cer` (all) | 0.0191 | **0.0113** | ≤ 0.005 |
| `cer_actualtext` | 0.0194 | **0.0** | |
| `cer_broken` (no ActualText) | 0.0194 | 0.0133 | |
| `cer_clean` (incl. DOCX, TXT) | 0.0142 | 0.0130 | |
| `cer_text` (text-layer cases) | 0.0758 | 0.0465 | |
| `valid_words_altered` | 0 | 0 | 0 (hard) |
| `hallucinated_values` | 0 | 0 | 0 (hard) |
| `correction_precision` | 1.0 | 1.0 | ≥ 0.99 |
| `field_precision` | 0.696 | **0.769** | ≥ 0.98 |
| `field_recall` | 0.362 | **0.396** | ≥ 0.95 |
| `misfiled` | 122 | 120 | 0 |
| `unrecognized_dropped` | 4 | 4 | 0 (hard) |
| `bullets_intact` | 0.830 | **0.929** | ≥ 0.98 |
| `quality_routing` | — | **1.0** | |

Gate: still **BLOCKED**, as planned. v2 is at least as good as current on every case.

Real PDFs (local, not scored: no ground truth yet): both resumes in `data/real/` (Pages, Calibri) read with no
broken word under v2 and route `clean`; today's reader leaves 49 and 54 differences in them, all errors.

## What moved

* **ActualText**: every ActualText PDF now reads with no wrong word (`Kafka`, `Software`, `https://`, `attention`).
  Both real PDFs in this install read with no broken word.
* **Line-break hyphens**: "produc-" / "tion" → "production"; "real-" / "time" keeps its hyphen
  (`text-line-break-hyphens` CER 0.43 → 0.0).
* **Commas and thousands separators** no longer break lines or numbers, so role headers parse more often (recall up on
  Ana, Ben, Devika).
* **Quality routing**: every clean and ActualText fixture routes `clean`, every broken one `poor`; the real PDFs route
  `clean`.

## What did not, and where it goes

* `cer_broken`: PDFs whose mapping is broken *without* ActualText still read `Kaga`, `So4ware`, `hCps` →
  Phase 6 (OCR of the words the quality report flags, corrections with evidence).
* `cer_text`: `Kaga`, `transformayon`, a single `So4ware`, digit glyphs (`Solu5ons`, `5me`) → Phase 6. The
  existing repair cannot see a digit inside a word (it treats digits as ordinary: "k8s").
* `misfiled`, `field_recall`, `unrecognized_dropped`: unrecognised headings, the sidebar read as the header, bullets
  without a "•" character, DOCX tables → Phase 3 (sections, DOCX).
