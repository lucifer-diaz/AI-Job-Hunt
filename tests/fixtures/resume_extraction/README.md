# resume_extraction fixtures

The evaluation data for resume upload → text → structure (`app/services/agent_eval/resume_extraction.py`,
`docs/resume-extraction-plan.md`).

| Path | What | Written by |
| --- | --- | --- |
| `resumes.json` | four synthetic resumes: the ground truth | a person |
| `dataset.json` | the cases: one per document, plus hand-written text-layer corruptions | builder (documents) + a person (text cases, version) |
| `documents/` | the documents the suite reads | `scripts/build_resume_fixtures.py` |
| `real/` | your own resumes, local only (see its README) | you |

Documents per resume: one layout each (Ana single column, Ben two columns, Chloe sidebar, Devika table), three
PDF variants each:

* `clean`: as Chromium printed it.
* `broken`: every ligature glyph's ToUnicode entry rewritten to one wrong character, as in the real Calibri PDF this
  work started from (ti → Y / Z, tf → [ / B, fk → g, ft → 4, tt → C, fi / fl → ﬁ / ﬂ).
* `actualtext`: broken, with `/ActualText` carrying the right text around each ligature glyph, as Apple Pages writes.

Plus `ana.docx`, `dev.docx` (skills in a table), `ben.txt`, `chloe.txt`. Headings differ per resume on purpose
(Skills / Core Skills / Areas of Expertise / Tech Stack, Experience / Work History / Career History / Professional
Experience).

Rebuild after editing `resumes.json`:

    pip install fonttools                     # dev dependency
    python scripts/build_resume_fixtures.py   # needs Inter (OFL) TTFs: INTER_FONT_DIR, default ~/Library/Fonts
    python scripts/build_resume_fixtures.py --check

Then bump `version` in `dataset.json`. The PDFs embed a subset of "Ligature Fixture Sans", a renamed derivative of
Inter (SIL Open Font License 1.1).
