# Resume extraction: Phase 6 (OCR and evidence-backed corrections)

Dataset `resume_extraction/dataset` v2 (`5ff9c659e12a`); Tesseract 5.5 installed (the numbers depend on it: without
an engine, broken PDFs are corrected by consistent rules alone).

    python scripts/agent_eval.py --suite resume_extraction --resume-pipeline v2 [--with-real-resumes]

| Metric | current | v2 (Phase 3–5) | **v2 (Phase 6)** | v2 + real | Target |
| --- | --- | --- | --- | --- | --- |
| Gate | BLOCKED | BLOCKED | **PASS** | **PASS** | |
| `cer` | 0.0191 | 0.0050 | **0.0018** | 0.0012 | ≤ 0.005 |
| `cer_broken` (no ActualText) | 0.0194 | 0.0133 | **0.0032** | | |
| `cer_text` (text layer only) | 0.0758 | 0.0465 | **0.0269** | | |
| `cer_clean` / `cer_actualtext` / `cer_real` | 0.0142 / 0.0194 / 0.0113 | 0 / 0 / 0 | 0 / 0 | 0 | |
| `field_precision` | 0.696 | 0.943 | **0.983** | 0.982 | ≥ 0.98 |
| `field_recall` | 0.362 | 0.930 | **0.983** | 0.985 | ≥ 0.95 |
| `misfiled` | 122 | 3 | **0** | 0 | 0 |
| `unrecognized_dropped` | 4 | 1 | **0** | 0 | 0 (hard) |
| `bullets_intact` | 0.830 | 0.929 | **0.991** | 0.994 | ≥ 0.98 |
| `correction_precision` | 1.0 | 1.0 | 1.0 | 1.0 | ≥ 0.99 |
| `valid_words_altered` / `hallucinated_values` | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 (hard) |

Corrections made on the broken fixtures (each recorded with its evidence): Kaga → Kafka, So4ware → Software,
li4ed → lifted, producYonize → productionize, aCribuYon → attribution, ExperZse → Expertise, PlaBorm → Platform,
EducaZon → Education, hCps → https, seCings → settings, Shi4 → Shift, OCawa → Ottawa, cuCing → cutting. Text-only
cases: transformayon / optimizayon / automayon and Solu5ons / opera5ons / crea5on / 5me by a rule each.

Not corrected, by design (a wrong correction is worse than a missed one): names no dictionary knows (WhiBield,
SeaCle, WingYp), short words (Yme) and a single unknown word with no page to read (Kaga in a text-only case,
PlaGorm). They stay as written and counted as suspicious.
