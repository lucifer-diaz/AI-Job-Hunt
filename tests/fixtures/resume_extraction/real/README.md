# Real resumes (local only)

Everything in this folder except this file and `.gitignore` is ignored by git. Nothing here is ever committed, and
the suite reads it only when asked:

    python scripts/agent_eval.py --suite resume_extraction --with-real-resumes

Add one pair per resume:

* `<name>.pdf` (or `.docx`, `.txt`): the document as uploaded.
* `<name>.json`: its ground truth, written by a person, in the same shape as one entry of
  `../resumes.json` (`name`, `headline`, `contact`, `headings`, `order`, `summary`, `skills`, `experience`,
  `education`, and `certifications` / `projects` when present). Mark terms no lexicon would know with
  `"recognized": false`.

Use anonymised resumes, or your own with your consent. Real cases are reported with the variant `real`, so their
character error rate shows up as `cer_real` beside the synthetic numbers.
